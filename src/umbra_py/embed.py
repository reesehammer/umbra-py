"""Archive scene embeddings: visual similarity search over the Umbra archive.

This is the exploratory C5 capability in ``docs/AI_INTEGRATION_IDEAS.md`` -- the
last open AI item, and the one that assumes an AI in the loop rather than merely
making the library legible to one. Everything before it searched by *metadata*:
:mod:`umbra_py.fuzzy` and :mod:`umbra_py.semantic` match a query against a task's
*name*; :mod:`umbra_py.catalog` filters by date, bbox and product type. None of
them can answer "find scenes that *look like* this one" -- a flooded field, a
crowded berth, a runway -- because that question lives in the pixels, not the
metadata. This module answers it.

The idea is the one the whole AI direction rests on (``§2``): the library's
outputs are *images with precise metadata*, the native input of a vision model.
:mod:`umbra_py.describe` had a model *read* one rendered quicklook; this embeds
*every* quicklook once into a vector and then ranks them by cosine similarity, so
image-to-image (:meth:`SceneEmbeddingIndex.similar_to_item`) and text-to-scene
(:meth:`SceneEmbeddingIndex.similar_to_text`, given a joint CLIP-family model)
search become plain, offline arithmetic over the stored vectors.

How it stays inside the determinism boundary
---------------------------------------------
The design mirrors :mod:`umbra_py.semantic` exactly, because the boundary is the
same. The *only* parts that consult a model are (a) turning an image into a
vector and (b) turning a text query into a vector. Both are injectable callables
(:data:`ImageEmbedder`, :data:`~umbra_py.semantic.Embedder`), so every test runs
against a deterministic stand-in and never touches the network. Everything else
-- rendering the quicklook (the same deterministic :func:`umbra_py.quicklook`
every command uses), storing the vectors, cosine ranking, thresholding -- is
offline and fully testable:

1. :meth:`SceneEmbeddingIndex.build` renders each acquisition's quicklook once,
   embeds it, and persists the vector keyed by item id (idempotent: an
   already-embedded item is skipped, so a rebuild only embeds what is new). The
   vectors live in their own small SQLite database beside the catalog index --
   ``catalog.db`` -> ``catalog.embed.db`` -- schema-versioned with
   ``PRAGMA user_version``, so the deterministic index and its published snapshot
   never carry model-derived data a core install can't use (the same reasoning
   :mod:`umbra_py.semantic` uses for the task-name sidecar).
2. :meth:`SceneEmbeddingIndex.similar` scores a query vector against every stored
   vector by :func:`umbra_py.semantic.cosine_similarity` in plain Python. At the
   current catalog scale (thousands of acquisitions) a brute-force scan is
   instant, so this adds no binary dependency (no ``sqlite-vec``, no ``numpy``);
   the schema leaves room to swap in a vector extension later without changing the
   public API.

The feature lives behind the ``[ai]`` extra (plus ``[viz]`` to render the
quicklooks it embeds) and never runs implicitly: only when a caller builds or
queries the scene index -- and only with a user-supplied embedding key -- does a
model get consulted. Provenance rides along too: a :class:`SceneMatch` is a
pointer back to a real acquisition (its id, task and STAC href), never a
model-authored fact, and the ranking is a measurement a test can recompute.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from array import array
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .exceptions import MissingDependencyError, UmbraError
from .models import UmbraItem
from .semantic import Embedder, cosine_similarity

__all__ = [
    "EmbedError",
    "SceneMatch",
    "SceneEmbeddingIndex",
    "ImageEmbedder",
    "Renderer",
    "default_scene_embed_path",
    "default_image_embedder",
    "default_text_embedder",
    "fetch_prebuilt_embeddings",
    "resolve_scene_model",
]

#: An image embedder turns a list of PNG-encoded images (bytes) into a list of
#: equal-length float vectors (one per image, in order). Injectable so tests never
#: call a model; the default implementation is :func:`default_image_embedder`.
ImageEmbedder = Callable[[list[bytes]], list[list[float]]]

#: A renderer turns an item into a PNG quicklook (bytes). Injectable so tests
#: never touch the network or need the ``viz`` extra. The default reuses
#: :func:`umbra_py.describe.render_quicklook_png` -- the same picture ``umbra
#: describe`` sends a model and a human sees.
Renderer = Callable[[UmbraItem], bytes]

#: Bump when the on-disk layout changes so an old database is detected on open
#: rather than misread. Stored via ``PRAGMA user_version``.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scene_vectors (
    item_id   TEXT PRIMARY KEY,
    href      TEXT,
    task      TEXT,
    datetime  TEXT,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL
);
"""


class EmbedError(UmbraError):
    """Raised when the scene index cannot answer -- an empty or model-mismatched
    index, an embedder that returns the wrong shape, or an unreadable database."""


@dataclass(frozen=True)
class SceneMatch:
    """One ranked acquisition: the ``item_id`` (and its ``task``, ``datetime`` and
    STAC ``href`` for context) and its cosine ``score`` against the query (``1.0``
    identical, ``0.0`` unrelated, higher is closer).

    A match is a pointer back to a real acquisition, never a model-authored fact:
    every field except ``score`` was recorded at build time from the deterministic
    item, and ``score`` is a measurement a test can recompute.
    """

    item_id: str
    score: float
    task: str | None = None
    datetime: str | None = None
    href: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "score": round(self.score, 6),
            "task": self.task,
            "datetime": self.datetime,
            "href": self.href,
        }


def default_scene_embed_path(index_path: str | os.PathLike | None = None) -> Path:
    """Where the scene-embedding database lives by default.

    It sits *beside* the catalog index (``catalog.db`` -> ``catalog.embed.db``) so
    the two travel together, while staying a separate file: the embedding layer is
    opt-in and model-backed, and keeping it out of ``catalog.db`` means the
    deterministic index (and its published snapshot) never carries vectors a core
    install can't use. Pass ``index_path`` to derive the sibling name from a
    non-default index location.
    """
    from .index import default_index_path

    base = Path(index_path) if index_path is not None else default_index_path()
    return base.with_name(f"{base.stem}.embed.db")


def fetch_prebuilt_embeddings(
    dest: str | os.PathLike | None = None,
    *,
    url: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download the published prebuilt scene-embedding sidecar.

    The weekly index workflow can ship a ``catalog.embed.db`` on the rolling
    ``catalog-index`` release beside ``catalog.db`` / ``catalog.pmtiles``, so a
    fresh install gets visual similarity search over the whole archive with no
    rebuild -- the embedding sibling of
    :meth:`umbra_py.index.CatalogIndex.from_release` and
    :func:`umbra_py.pmtiles.fetch_prebuilt_pmtiles`. This fetches that sidecar
    straight to ``dest`` (default: :func:`default_scene_embed_path`) and returns
    its path. Re-run any time to refresh; the download is resume-safe and always
    overwrites the existing file. ``url`` overrides the release asset location
    (e.g. to pull from a fork or a mirror). Open the result with
    :class:`SceneEmbeddingIndex` (or use :meth:`SceneEmbeddingIndex.from_release`,
    which wraps this), then query it with the matching embedding model.
    """
    from .constants import CATALOG_INDEX_EMBED_URL
    from .download import download_url  # local dependency; keep the import cheap

    target = Path(dest) if dest is not None else default_scene_embed_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    download_url(url or CATALOG_INDEX_EMBED_URL, target, overwrite=True, progress=progress)
    return target


def _vector_bytes(vec: Sequence[float]) -> bytes:
    """Pack a float vector as little-endian float32 bytes for BLOB storage."""
    return array("f", vec).tobytes()


def _vector_from_bytes(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


class SceneEmbeddingIndex:
    """An embedding index over rendered Umbra quicklooks for visual similarity.

    Open (creating the database and schema if needed) with a path, or no path to
    use :func:`default_scene_embed_path`. Usable as a context manager, which
    commits and closes on exit::

        from umbra_py.embed import SceneEmbeddingIndex, default_image_embedder
        from umbra_py import UmbraCatalog

        embed = default_image_embedder()               # needs an embedding API key
        items = list(UmbraCatalog().search(area="Centerfield", limit=50))
        with SceneEmbeddingIndex() as idx:
            idx.build(items, embedder=embed)           # render + embed once
            for m in idx.similar_to_item(items[0], embedder=embed):
                print(m.item_id, round(m.score, 3))    # scenes that look alike

    The model is consulted only to embed an image or a text query (the injected
    ``embedder``); rendering, storage, ranking and thresholding are deterministic
    and offline.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else default_scene_embed_path()
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
            raise EmbedError(
                f"Scene index at {self.path} has schema version {version}, but this "
                f"umbra-py expects {_SCHEMA_VERSION}. Delete it and rebuild with "
                "'umbra embed build'."
            )

    @classmethod
    def from_release(
        cls,
        path: str | os.PathLike | None = None,
        *,
        url: str | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> SceneEmbeddingIndex:
        """Download the published prebuilt scene-embedding sidecar and open it.

        Embedding every quicklook in the archive is the one expensive step of
        visual similarity search -- it renders each scene and calls a model. This
        skips it: it fetches the published ``catalog.embed.db`` from the project's
        rolling ``catalog-index`` GitHub release straight to ``path`` (default:
        :func:`default_scene_embed_path`) and returns an open index over it, so
        ``similar_to_item`` / ``similar_to_text`` work with **no rebuild** -- the
        embedding sibling of :meth:`umbra_py.index.CatalogIndex.from_release` and
        :func:`umbra_py.pmtiles.fetch_prebuilt_pmtiles`. Only the query itself
        still needs an embedding key (the archive vectors arrive pre-built).

        The download is resume-safe and always overwrites the existing file; re-run
        any time to refresh. ``url`` overrides the release asset location (e.g. to
        pull from a fork or a mirror). Because the vectors are model-specific, the
        published table's :meth:`stored_model` records the embedding model it was
        built with -- query it with the matching model (see :meth:`similar`).
        """
        target = fetch_prebuilt_embeddings(path, url=url, progress=progress)
        return cls(target)

    # -- lifecycle -------------------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> SceneEmbeddingIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM scene_vectors").fetchone()[0]

    def __contains__(self, item_id: object) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM scene_vectors WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def stored_model(self) -> str | None:
        """The embedding model the stored vectors were produced with, or ``None``
        for an empty index. Mixing models in one index is disallowed (see
        :meth:`build`), so this is single-valued."""
        row = self._conn.execute("SELECT model FROM scene_vectors LIMIT 1").fetchone()
        return row[0] if row else None

    # -- writing ---------------------------------------------------------------

    def build(
        self,
        items: Iterable[UmbraItem],
        *,
        embedder: ImageEmbedder,
        render: Renderer | None = None,
        model: str = "default",
        batch_size: int = 16,
        skip_render_errors: bool = True,
        progress: Callable[[int, int], None] | None = None,
        on_error: Callable[[UmbraItem, Exception], None] | None = None,
    ) -> int:
        """Render and embed each item's quicklook, persisting one vector per item.

        Returns the number newly embedded (an item already in the index is
        skipped, so a rebuild is cheap). ``render`` turns an item into PNG bytes
        (default :func:`_render_quicklook`, requiring the ``viz`` extra);
        ``embedder`` turns a batch of those PNGs into vectors. ``model`` is a label
        recorded with each vector so a query can refuse to compare across embedding
        models; all vectors in one index must share it (rebuild in a fresh file to
        switch models). ``embedder`` is called in batches of ``batch_size``;
        ``progress`` (if given) receives ``(done, total)`` after each batch.

        Rendering streams overviews over the network and can fail for one bad asset
        without dooming the batch: with ``skip_render_errors`` (the default) an
        item whose quicklook won't render is skipped and passed to ``on_error``
        (if given) rather than aborting the build.
        """
        render_fn = render or _render_quicklook
        existing_model = self.stored_model()
        if existing_model is not None and existing_model != model:
            raise EmbedError(
                f"This index already holds vectors from model {existing_model!r}; "
                f"refusing to mix in {model!r}. Rebuild in a fresh file to switch models."
            )

        # De-dupe by id and drop items already embedded, so a rebuild only renders
        # and embeds the new acquisitions.
        todo: list[UmbraItem] = []
        seen_ids: set[str] = set()
        for item in items:
            if not item.id or item.id in seen_ids or item.id in self:
                continue
            seen_ids.add(item.id)
            todo.append(item)
        total = len(todo)

        written = 0
        step = max(1, batch_size)
        for start in range(0, total, step):
            chunk = todo[start : start + step]
            rendered: list[UmbraItem] = []
            pngs: list[bytes] = []
            for item in chunk:
                try:
                    png = render_fn(item)
                except (MissingDependencyError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    if not skip_render_errors:
                        raise EmbedError(
                            f"Could not render a quicklook of {item.id}: {exc}"
                        ) from exc
                    if on_error is not None:
                        on_error(item, exc)
                    continue
                rendered.append(item)
                pngs.append(png)

            if pngs:
                vectors = embedder(pngs)
                if len(vectors) != len(pngs):
                    raise EmbedError(
                        f"Embedder returned {len(vectors)} vectors for {len(pngs)} images."
                    )
                for item, vec in zip(rendered, vectors, strict=True):
                    vec = list(vec)
                    if not vec:
                        raise EmbedError(f"Embedder returned an empty vector for {item.id!r}.")
                    dt = item.datetime.isoformat() if item.datetime else None
                    self._conn.execute(
                        "INSERT OR REPLACE INTO scene_vectors "
                        "(item_id, href, task, datetime, model, dim, vec) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (item.id, item.href, item.task, dt, model, len(vec), _vector_bytes(vec)),
                    )
                    written += 1
                self._conn.commit()
            if progress is not None:
                progress(min(start + step, total), total)
        return written

    # -- querying --------------------------------------------------------------

    def similar(
        self,
        query_vec: Sequence[float],
        *,
        top_k: int = 10,
        min_score: float = 0.0,
        exclude_id: str | None = None,
    ) -> list[SceneMatch]:
        """Rank the stored scenes by cosine similarity to ``query_vec``.

        Returns the ``top_k`` matches with score ``>= min_score``, highest first
        (empty if nothing clears the threshold). ``exclude_id`` drops that item
        from the results -- used by :meth:`similar_to_item` so a scene never
        returns itself as its own best match. Raises :class:`EmbedError` if the
        index is empty (build it first) or ``query_vec``'s length disagrees with
        the stored vectors (a model mismatch).
        """
        rows = self._conn.execute(
            "SELECT item_id, task, datetime, href, dim, vec FROM scene_vectors"
        ).fetchall()
        if not rows:
            raise EmbedError("The scene index is empty. Build it first with 'umbra embed build'.")
        qvec = list(query_vec)
        if not qvec:
            raise EmbedError("The query vector is empty.")
        scored: list[SceneMatch] = []
        for item_id, task, dt, href, dim, blob in rows:
            if item_id == exclude_id:
                continue
            if dim != len(qvec):
                raise EmbedError(
                    f"Query embedding has length {len(qvec)} but stored vectors have "
                    f"length {dim} -- the index was built with a different embedding "
                    "model. Rebuild it with the model you are querying with."
                )
            score = cosine_similarity(qvec, _vector_from_bytes(blob))
            if score >= min_score:
                scored.append(
                    SceneMatch(item_id=item_id, score=score, task=task, datetime=dt, href=href)
                )
        scored.sort(key=lambda m: (-m.score, m.item_id))
        return scored[: max(0, top_k)]

    def similar_to_item(
        self,
        item: UmbraItem,
        *,
        embedder: ImageEmbedder,
        render: Renderer | None = None,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SceneMatch]:
        """Find stored scenes that look like ``item``.

        Renders ``item``'s quicklook, embeds it, and ranks the stored vectors by
        :meth:`similar`. The query item is excluded from its own results by id, so
        an already-indexed scene does not rank itself first. The render and the
        embedding are the only model/network touch points and both are injectable.
        """
        render_fn = render or _render_quicklook
        png = render_fn(item)
        vectors = embedder([png])
        if not vectors or not vectors[0]:
            raise EmbedError("Embedder returned no vector for the query image.")
        return self.similar(vectors[0], top_k=top_k, min_score=min_score, exclude_id=item.id)

    def similar_to_text(
        self,
        query: str,
        text_embedder: Embedder,
        *,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[SceneMatch]:
        """Find stored scenes that match a plain-language ``query`` ("a flooded
        field", "ships at a berth").

        Embeds the text with ``text_embedder`` and ranks the stored *image*
        vectors by :meth:`similar`. This only works when the text and image vectors
        live in the **same** space -- i.e. the index was built and this query is
        embedded with a joint CLIP-family model (see :func:`default_text_embedder`
        / :func:`default_image_embedder`). A model whose text encoder has a
        different dimensionality is caught by :meth:`similar` as a mismatch; a
        same-dim but non-joint model would return meaningless scores, so pairing
        the two is the caller's responsibility (and the ``model`` label records
        which one built the index).
        """
        vectors = text_embedder([query])
        if not vectors or not vectors[0]:
            raise EmbedError("Text embedder returned no vector for the query.")
        return self.similar(vectors[0], top_k=top_k, min_score=min_score)

    def stats(self) -> dict[str, object]:
        """Summary for ``umbra embed info``: how many scene vectors are stored, the
        embedding model they came from, and their dimensionality."""
        row = self._conn.execute("SELECT COUNT(*), MAX(dim) FROM scene_vectors").fetchone()
        return {"scenes": row[0], "model": self.stored_model(), "dim": row[1]}


# --- Rendering the scene (deterministic; the picture that gets embedded) -----


def _render_quicklook_asset(item: UmbraItem, *, asset: str = "GEC") -> bytes:
    """Render an item's quicklook to PNG bytes for embedding.

    Reuses :func:`umbra_py.describe.render_quicklook_png` so a scene is embedded
    from exactly the picture a human (and ``umbra describe``) sees. ``asset``
    selects which product to render (default the geocoded ``GEC``). Requires the
    ``viz`` extra.
    """
    from .describe import render_quicklook_png

    return render_quicklook_png(item, asset=asset)


def _render_quicklook(item: UmbraItem) -> bytes:
    """Default :data:`Renderer`: the GEC quicklook, embedded as PNG bytes."""
    return _render_quicklook_asset(item, asset="GEC")


# --- The model boundary (the only parts that call a model) -------------------


def _post_json(url: str, headers: dict[str, str], payload: dict) -> dict:
    import requests  # a core dependency; imported here to keep the module light

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code >= 400:
        raise EmbedError(
            f"The embedding endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def _openai_multimodal_embed(
    *, api_key: str, model: str, base_url: str, inputs: list
) -> list[list[float]]:
    """POST an OpenAI-compatible ``/embeddings`` request and return the vectors.

    Shared by the image and text embedders: the only difference is what goes in
    ``input`` (base64 data URIs for images, plain strings for text). The response
    is the standard ``{"data": [{"index", "embedding"}]}`` shape; we sort by index
    rather than trusting response order.
    """
    if not inputs:
        return []
    data = _post_json(
        f"{base_url.rstrip('/')}/embeddings",
        {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        {"model": model, "input": inputs},
    )
    try:
        items = sorted(data["data"], key=lambda d: d["index"])
        return [item["embedding"] for item in items]
    except (KeyError, TypeError) as exc:
        raise EmbedError(f"Unexpected embeddings response shape: {exc}") from exc


def _openai_image_embedder(*, api_key: str, model: str, base_url: str) -> ImageEmbedder:
    def embed(images: list[bytes]) -> list[list[float]]:
        data_uris = [
            f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}" for png in images
        ]
        return _openai_multimodal_embed(
            api_key=api_key, model=model, base_url=base_url, inputs=data_uris
        )

    return embed


def _openai_text_embedder(*, api_key: str, model: str, base_url: str) -> Embedder:
    def embed(texts: list[str]) -> list[list[float]]:
        return _openai_multimodal_embed(
            api_key=api_key, model=model, base_url=base_url, inputs=list(texts)
        )

    return embed


def resolve_scene_model(model: str | None = None) -> str:
    """The scene-embedding model name the default embedders would use.

    Resolves the explicit ``model`` argument, else ``$UMBRA_SCENE_EMBED_MODEL``,
    else the ``clip`` default -- the single source of truth the CLI passes to
    :meth:`SceneEmbeddingIndex.build` as the stored-vector label, so the label
    matches the model the vectors were actually produced with.
    """
    return model or os.environ.get("UMBRA_SCENE_EMBED_MODEL") or "clip"


def _require_embed_key(model: str) -> tuple[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingDependencyError(
            "umbra embed needs a multimodal (CLIP-family) embedding API key. Set "
            "OPENAI_API_KEY (and optionally OPENAI_BASE_URL for an OpenAI-compatible "
            f"/embeddings endpoint, or UMBRA_SCENE_EMBED_MODEL to pick the model; "
            f"default {model!r}). Text-to-scene search additionally needs the model's "
            "text and image encoders to share a space. The library still runs every "
            "search deterministically; embeddings only rank scenes by appearance.",
            hint="Set OPENAI_API_KEY",
        )
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    return api_key, base_url


def default_image_embedder(*, model: str | None = None) -> ImageEmbedder:
    """Build an :data:`ImageEmbedder` from environment variables.

    Uses an OpenAI-compatible ``/embeddings`` endpoint that accepts images as
    base64 ``data:`` URIs in the ``input`` field -- the shape served by CLIP-family
    multimodal embedding backends (LocalAI, Jina, and similar OpenAI-compatible
    gateways) -- talking plain HTTPS with the already-core :mod:`requests`, no
    heavy SDK:

    - ``OPENAI_API_KEY`` is the key; ``OPENAI_BASE_URL`` overrides the host (e.g. a
      local CLIP server or proxy endpoint), default ``https://api.openai.com/v1``.
    - ``UMBRA_SCENE_EMBED_MODEL`` (or the ``model=`` argument / ``--model`` flag)
      chooses the model, default ``clip``.

    Raises :class:`umbra_py.MissingDependencyError` with setup guidance when no key
    is configured -- the embedding layer never reaches a model without an explicit,
    user-supplied key. The name recorded with each stored vector is the resolved
    model, so a later query refuses to compare across models.
    """
    model = resolve_scene_model(model)
    api_key, base_url = _require_embed_key(model)
    return _openai_image_embedder(api_key=api_key, model=model, base_url=base_url)


def default_text_embedder(*, model: str | None = None) -> Embedder:
    """Build a text :data:`~umbra_py.semantic.Embedder` for text-to-scene search.

    Uses the **same** OpenAI-compatible ``/embeddings`` endpoint and the **same**
    model as :func:`default_image_embedder` (a joint CLIP-family model whose text
    and image encoders share a vector space), differing only in that it sends plain
    text in ``input``. Pairing it with an image index built by a *different* model
    would return meaningless scores, so the resolved model name is what the scene
    index records as its label. Raises :class:`umbra_py.MissingDependencyError`
    when no key is configured.
    """
    model = resolve_scene_model(model)
    api_key, base_url = _require_embed_key(model)
    return _openai_text_embedder(api_key=api_key, model=model, base_url=base_url)
