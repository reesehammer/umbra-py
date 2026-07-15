"""Semantic task-name aliasing: the embedding-backed layer of natural-language
search that plain string similarity can't (and shouldn't) fake.

This is the last open piece of the C1 natural-language-search plan in
``docs/AI_INTEGRATION_IDEAS.md``. The three earlier steps stay inside the
library's determinism boundary: relative dates (:mod:`umbra_py.dates`) and the
token-wise fuzzy matcher (:mod:`umbra_py.fuzzy`) turn language into a filter with
**no model call**, and ``umbra ask`` (:mod:`umbra_py.planner`) lets a model
*plan* a search the deterministic layer then re-validates. What none of them
reach is a query whose words never appear in the task label at all:

    "grain storage north dakota"  ->  "Beet Piler - ND"

The task name shares no token with the query -- a beet piler *is* grain-adjacent
agricultural storage, but only a model that has read about the world knows that.
:mod:`umbra_py.fuzzy` deliberately does not guess here (it would either miss the
match or admit false positives); this module answers it the honest way, with an
**embedding index** over the task names.

How it stays inside the determinism boundary
---------------------------------------------
The *only* part that consults a model is turning text into a vector. That is an
injectable :data:`Embedder` callable, exactly like the injectable
:data:`~umbra_py.planner.Planner`, so every test runs against a deterministic
stand-in embedder and never touches the network. Everything else -- storing the
vectors, cosine similarity, ranking, thresholding -- is stdlib-only, offline, and
fully testable:

1. :meth:`SemanticTaskIndex.build` embeds each distinct task name once and
    persists the vector (idempotent: an already-embedded name is skipped, so a
    rebuild only embeds what is new). The vectors live in their own small
    SQLite database beside the catalog index, schema-versioned with
    ``PRAGMA user_version`` so a future format change is detectable rather than
    silently corrupting.
2. :meth:`SemanticTaskIndex.matching_tasks` embeds the *query* once, then ranks
    the stored task vectors by cosine similarity in plain Python. At the current
    catalog scale (a few thousand tasks) a brute-force scan is instant, so this
    adds no binary dependency (no ``sqlite-vec``, no ``numpy``); the storage
    schema leaves room to swap in a vector extension later without changing the
    public API.

The feature lives behind the ``[ai]`` extra and never runs implicitly: only when
a caller builds or queries the semantic index -- and only with a user-supplied
embedding key -- does a model get consulted. The deterministic matchers remain
the default search path; this is the optional layer on top of them.
"""

from __future__ import annotations

import math
import os
import sqlite3
from array import array
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .exceptions import MissingDependencyError, UmbraError

__all__ = [
    "SemanticError",
    "SemanticMatch",
    "SemanticTaskIndex",
    "Embedder",
    "cosine_similarity",
    "default_embedder",
    "default_semantic_path",
    "resolve_embed_model",
]

#: An embedder turns a list of texts into a list of equal-length float vectors
#: (one per text, in order). Injectable so tests never call a model; the default
#: implementation is :func:`default_embedder`.
Embedder = Callable[[list[str]], list[list[float]]]

#: Bump when the on-disk layout changes so an old database is detected on open
#: rather than misread. Stored via ``PRAGMA user_version``.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_vectors (
    task   TEXT PRIMARY KEY,
    model  TEXT NOT NULL,
    dim    INTEGER NOT NULL,
    vec    BLOB NOT NULL
);
"""


class SemanticError(UmbraError):
    """Raised when the semantic index cannot answer -- an empty or model-mismatched
    index, an embedder that returns the wrong shape, or an unreadable database."""


@dataclass(frozen=True)
class SemanticMatch:
    """One ranked candidate: an Umbra ``task`` name and its cosine ``score``
    against the query (``1.0`` identical, ``0.0`` unrelated, higher is closer)."""

    task: str
    score: float


def default_semantic_path(index_path: str | os.PathLike | None = None) -> Path:
    """Where the semantic vector database lives by default.

    It sits *beside* the catalog index (``catalog.db`` -> ``catalog.semantic.db``)
    so the two travel together, while staying a separate file: the semantic layer
    is opt-in and model-backed, and keeping it out of ``catalog.db`` means the
    deterministic index (and its published snapshot) never carries embeddings a
    core install can't use. Pass ``index_path`` to derive the sibling name from a
    non-default index location.
    """
    from .index import default_index_path

    base = Path(index_path) if index_path is not None else default_index_path()
    return base.with_name(f"{base.stem}.semantic.db")


def _vector_bytes(vec: list[float]) -> bytes:
    """Pack a float vector as little-endian float32 bytes for BLOB storage."""
    return array("f", vec).tobytes()


def _vector_from_bytes(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def cosine_similarity(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity of two equal-length vectors, in plain Python.

    Returns ``0.0`` if either vector is all-zero (undefined direction) rather
    than dividing by zero. Raises :class:`SemanticError` on a length mismatch --
    comparing vectors from different embedding models is a bug, not a 0.
    """
    av = list(a)
    bv = list(b)
    if len(av) != len(bv):
        raise SemanticError(
            f"Cannot compare vectors of length {len(av)} and {len(bv)} -- "
            "they were produced by different embedding models."
        )
    dot = sum(x * y for x, y in zip(av, bv, strict=True))
    na = math.sqrt(sum(x * x for x in av))
    nb = math.sqrt(sum(y * y for y in bv))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SemanticTaskIndex:
    """An embedding index over Umbra task names for semantic ``area`` matching.

    Open (creating the database and schema if needed) with a path, or no path to
    use :func:`default_semantic_path`. Usable as a context manager, which commits
    and closes on exit::

        from umbra_py.semantic import SemanticTaskIndex, default_embedder

        embed = default_embedder()                 # needs an embedding API key
        with SemanticTaskIndex() as sem:
            sem.build(index_path="catalog.db", embedder=embed)     # embed once
            for match in sem.matching_tasks("grain storage north dakota", embed):
                print(match.task, round(match.score, 3))           # Beet Piler - ND ...

    The model is consulted only to embed text (the injected ``embedder``);
    storage, ranking and thresholding are deterministic and offline.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else default_semantic_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._init_schema()

    def _init_schema(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Fresh (or pre-versioning) database: create the schema and stamp it.
            self._conn.executescript(_SCHEMA)
            self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            self._conn.commit()
        elif version != _SCHEMA_VERSION:
            self._conn.close()
            raise SemanticError(
                f"Semantic index at {self.path} has schema version {version}, but "
                f"this umbra-py expects {_SCHEMA_VERSION}. Delete it and rebuild "
                "with 'umbra semantic build'."
            )

    # -- lifecycle -------------------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> SemanticTaskIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM task_vectors").fetchone()[0]

    # -- reading the task list -------------------------------------------------

    @staticmethod
    def _tasks_from_index(index_path: str | os.PathLike | None) -> list[str]:
        """Read the distinct, non-null task names from a catalog index database.

        This is how the semantic layer "builds on" the deterministic index: the
        same task labels the fuzzy matcher scans are what gets embedded.
        """
        from .index import default_index_path

        path = Path(index_path) if index_path is not None else default_index_path()
        if not path.exists():
            raise SemanticError(
                f"No catalog index at {path}. Build or fetch one first with "
                "'umbra index build' / 'umbra index fetch'."
            )
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT DISTINCT task FROM items WHERE task IS NOT NULL AND task != '' "
                "ORDER BY task"
            ).fetchall()
        except sqlite3.OperationalError as exc:  # not a catalog index
            raise SemanticError(f"{path} is not a readable catalog index: {exc}") from exc
        finally:
            conn.close()
        return [row[0] for row in rows]

    def stored_model(self) -> str | None:
        """The embedding model the stored vectors were produced with, or ``None``
        for an empty index. Mixing models in one index is disallowed (see
        :meth:`build`), so this is single-valued."""
        row = self._conn.execute("SELECT model FROM task_vectors LIMIT 1").fetchone()
        return row[0] if row else None

    # -- writing ---------------------------------------------------------------

    def build(
        self,
        *,
        embedder: Embedder,
        task_names: Iterable[str] | None = None,
        index_path: str | os.PathLike | None = None,
        model: str = "default",
        batch_size: int = 128,
        progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Embed task names and persist their vectors. Returns the number newly
        embedded (an already-stored name is skipped, so a rebuild is cheap).

        Provide the names explicitly via ``task_names`` or let them be read from a
        catalog index (``index_path``, default :func:`~umbra_py.index.default_index_path`).
        ``model`` is a label recorded with each vector so a query can refuse to
        compare across embedding models; all vectors in one index must share it
        (rebuild in a fresh file to switch models). ``embedder`` is called in
        batches of ``batch_size``; ``progress`` (if given) receives
        ``(done, total)`` after each batch.
        """
        names = list(task_names) if task_names is not None else self._tasks_from_index(index_path)
        existing_model = self.stored_model()
        if existing_model is not None and existing_model != model:
            raise SemanticError(
                f"This index already holds vectors from model {existing_model!r}; "
                f"refusing to mix in {model!r}. Rebuild in a fresh file to switch models."
            )
        have = {row[0] for row in self._conn.execute("SELECT task FROM task_vectors")}
        todo = [name for name in dict.fromkeys(names) if name not in have]
        total = len(todo)
        written = 0
        for start in range(0, total, max(1, batch_size)):
            batch = todo[start : start + max(1, batch_size)]
            vectors = embedder(batch)
            if len(vectors) != len(batch):
                raise SemanticError(
                    f"Embedder returned {len(vectors)} vectors for {len(batch)} texts."
                )
            for name, vec in zip(batch, vectors, strict=True):
                vec = list(vec)
                if not vec:
                    raise SemanticError(f"Embedder returned an empty vector for {name!r}.")
                self._conn.execute(
                    "INSERT OR REPLACE INTO task_vectors (task, model, dim, vec) "
                    "VALUES (?, ?, ?, ?)",
                    (name, model, len(vec), _vector_bytes(vec)),
                )
                written += 1
            self._conn.commit()
            if progress is not None:
                progress(written, total)
        return written

    # -- querying --------------------------------------------------------------

    def matching_tasks(
        self,
        query: str,
        embedder: Embedder,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[SemanticMatch]:
        """Rank the stored task names by semantic closeness to ``query``.

        Embeds ``query`` once with ``embedder``, scores every stored vector by
        :func:`cosine_similarity`, and returns the ``top_k`` matches with score
        ``>= min_score``, highest first. Returns an empty list if nothing clears
        the threshold. Raises :class:`SemanticError` if the index is empty (build
        it first) or the query embedding's length disagrees with the stored
        vectors (a model mismatch).
        """
        rows = self._conn.execute("SELECT task, dim, vec FROM task_vectors").fetchall()
        if not rows:
            raise SemanticError(
                "The semantic index is empty. Build it first with 'umbra semantic build'."
            )
        vectors = embedder([query])
        if not vectors or not vectors[0]:
            raise SemanticError("Embedder returned no vector for the query.")
        qvec = list(vectors[0])
        scored: list[SemanticMatch] = []
        for task, dim, blob in rows:
            if dim != len(qvec):
                raise SemanticError(
                    f"Query embedding has length {len(qvec)} but stored vectors have "
                    f"length {dim} -- the index was built with a different embedding "
                    "model. Rebuild it with the model you are querying with."
                )
            score = cosine_similarity(qvec, _vector_from_bytes(blob))
            if score >= min_score:
                scored.append(SemanticMatch(task=task, score=score))
        scored.sort(key=lambda m: (-m.score, m.task))
        return scored[: max(0, top_k)]

    def stats(self) -> dict[str, object]:
        """Summary for ``umbra semantic info``: how many task vectors are stored,
        the embedding model they came from, and their dimensionality."""
        row = self._conn.execute("SELECT COUNT(*), MAX(dim) FROM task_vectors").fetchone()
        return {"tasks": row[0], "model": self.stored_model(), "dim": row[1]}


# --- The model boundary (the only part that calls a model) ------------------


def _post_json(url: str, headers: dict[str, str], payload: dict) -> dict:
    import requests  # a core dependency; imported here to keep the module light

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        raise SemanticError(
            f"The embedding endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def _openai_embedder(*, api_key: str, model: str, base_url: str) -> Embedder:
    def embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = _post_json(
            f"{base_url.rstrip('/')}/embeddings",
            {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            {"model": model, "input": texts},
        )
        try:
            # The API returns embeddings in an "index"-tagged list; sort to be
            # safe rather than trusting response order.
            items = sorted(data["data"], key=lambda d: d["index"])
            return [item["embedding"] for item in items]
        except (KeyError, TypeError) as exc:
            raise SemanticError(f"Unexpected embeddings response shape: {exc}") from exc

    return embed


def resolve_embed_model(model: str | None = None) -> str:
    """The embedding model name that :func:`default_embedder` would use.

    Resolves the explicit ``model`` argument, else ``$UMBRA_EMBED_MODEL``, else
    the ``text-embedding-3-small`` default -- the single source of truth the CLI
    passes to :meth:`SemanticTaskIndex.build` as the stored-vector label, so the
    label matches the model the vectors were actually produced with.
    """
    return model or os.environ.get("UMBRA_EMBED_MODEL") or "text-embedding-3-small"


def default_embedder(*, model: str | None = None) -> Embedder:
    """Build an :data:`Embedder` from environment variables.

    Uses an OpenAI-compatible ``/embeddings`` endpoint (the de-facto standard,
    served by OpenAI, many local runners, and proxy gateways), talking plain
    HTTPS with the already-core :mod:`requests` -- no heavy SDK:

    - ``OPENAI_API_KEY`` is the key; ``OPENAI_BASE_URL`` overrides the host (e.g.
      a local or proxy endpoint), default ``https://api.openai.com/v1``.
    - ``UMBRA_EMBED_MODEL`` (or the ``model=`` argument / ``--model`` flag)
      chooses the model, default ``text-embedding-3-small``.

    Raises :class:`umbra_py.MissingDependencyError` with setup guidance when no
    key is configured -- the semantic layer never reaches a model without an
    explicit, user-supplied key. The name recorded with each stored vector is the
    resolved model, so a later query refuses to compare across models.
    """
    model = resolve_embed_model(model)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingDependencyError(
            "umbra semantic needs an embedding API key. Set OPENAI_API_KEY (and "
            "optionally OPENAI_BASE_URL for an OpenAI-compatible endpoint, or "
            "UMBRA_EMBED_MODEL to pick the model). Embeddings only rank task "
            "names; the library still runs the resolved search deterministically."
        )
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return _openai_embedder(api_key=api_key, model=model, base_url=base_url)
