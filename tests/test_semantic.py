"""Offline tests for semantic task-name aliasing (:mod:`umbra_py.semantic`).

No test calls a model: the embedding step is an injectable callable, so a
deterministic *concept* embedder stands in for a real one. It maps a small
vocabulary of related words onto shared dimensions, so the query "grain storage
north dakota" and the label "Beet Piler - ND" land close in vector space (both
touch the agriculture-storage and Dakota concepts) while an unrelated label
("Port of Long Beach") does not -- exactly the aliasing a real embedder buys,
made deterministic and network-free. Everything the module does with those
vectors (storage, cosine ranking, thresholding, the CLI wiring) is what is
actually under test.
"""

from __future__ import annotations

import json
import math

import pytest
from click.testing import CliRunner

import umbra_py.semantic as semantic_mod
from umbra_py.cli import cli
from umbra_py.exceptions import MissingDependencyError
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem
from umbra_py.semantic import (
    SemanticError,
    SemanticMatch,
    SemanticTaskIndex,
    cosine_similarity,
    default_embedder,
    resolve_embed_model,
)

# --- A deterministic stand-in embedder --------------------------------------

# Each word maps to a set of "concept" dimensions. Related words (grain/beet/
# storage/piler) share dimensions so their vectors point the same way; the
# embedder is a normalized bag of concepts, so word order is irrelevant.
_CONCEPTS = ["agriculture", "storage", "dakota", "coast", "port", "military"]
_WORD_CONCEPTS: dict[str, list[str]] = {
    "grain": ["agriculture"],
    "beet": ["agriculture"],
    "storage": ["storage"],
    "piler": ["storage", "agriculture"],
    "silo": ["storage", "agriculture"],
    "north": ["dakota"],
    "dakota": ["dakota"],
    "nd": ["dakota"],
    "port": ["port", "coast"],
    "harbor": ["port", "coast"],
    "beach": ["coast"],
    "long": [],
    "base": ["military"],
    "airfield": ["military"],
}

_TOKEN_SPLIT = __import__("re").compile(r"[a-z0-9]+")


def _concept_vector(text: str) -> list[float]:
    weights = dict.fromkeys(_CONCEPTS, 0.0)
    for token in _TOKEN_SPLIT.findall(text.lower()):
        for concept in _WORD_CONCEPTS.get(token, []):
            weights[concept] += 1.0
    return [weights[c] for c in _CONCEPTS]


def concept_embedder(texts: list[str]) -> list[list[float]]:
    """A deterministic embedder: normalized bag-of-concepts, one vector per text."""
    return [_concept_vector(t) for t in texts]


_TASKS = ["Beet Piler - ND", "Port of Long Beach", "Grand Forks Airfield"]


# --- cosine_similarity ------------------------------------------------------


def test_cosine_identical_and_orthogonal():
    assert cosine_similarity([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero_not_error():
    # An all-zero vector has no direction; define similarity as 0 rather than
    # dividing by zero (a task label with no known concept words hits this).
    assert cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0


def test_cosine_length_mismatch_raises():
    with pytest.raises(SemanticError):
        cosine_similarity([1, 2], [1, 2, 3])


def test_cosine_matches_hand_computation():
    a, b = [1.0, 2.0, 2.0], [2.0, 0.0, 1.0]
    expected = (1 * 2 + 2 * 0 + 2 * 1) / (math.sqrt(9) * math.sqrt(5))
    assert cosine_similarity(a, b) == pytest.approx(expected)


# --- build + storage round-trip ---------------------------------------------


def test_build_embeds_and_persists(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        written = sem.build(embedder=concept_embedder, task_names=_TASKS)
        assert written == len(_TASKS)
        assert len(sem) == len(_TASKS)


def test_build_is_idempotent(tmp_path):
    path = tmp_path / "sem.db"
    with SemanticTaskIndex(path) as sem:
        assert sem.build(embedder=concept_embedder, task_names=_TASKS) == 3
        # Re-running embeds nothing new; only a new name is embedded.
        assert sem.build(embedder=concept_embedder, task_names=_TASKS) == 0
        assert sem.build(embedder=concept_embedder, task_names=[*_TASKS, "New Site"]) == 1
        assert len(sem) == 4


def test_build_survives_reopen(tmp_path):
    path = tmp_path / "sem.db"
    with SemanticTaskIndex(path) as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS)
    with SemanticTaskIndex(path) as sem:
        assert len(sem) == len(_TASKS)
        assert sem.stored_model() == "default"


def test_build_rejects_mixed_models(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS, model="model-a")
        with pytest.raises(SemanticError, match="refusing to mix"):
            sem.build(embedder=concept_embedder, task_names=["Other"], model="model-b")


def test_build_rejects_bad_embedder_shape(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        with pytest.raises(SemanticError, match="vectors for"):
            sem.build(embedder=lambda texts: [[1.0, 2.0]], task_names=["a", "b"])


def test_schema_version_is_stamped(tmp_path):
    import sqlite3

    path = tmp_path / "sem.db"
    with SemanticTaskIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == semantic_mod._SCHEMA_VERSION
    finally:
        conn.close()


def test_future_schema_version_is_rejected(tmp_path):
    import sqlite3

    path = tmp_path / "sem.db"
    with SemanticTaskIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(SemanticError, match="schema version 999"):
        SemanticTaskIndex(path)


# --- matching_tasks: the point of the feature -------------------------------


def test_semantic_alias_ranks_the_meant_task_first(tmp_path):
    # The query shares NO word with "Beet Piler - ND", yet it should rank first:
    # this is exactly what the deterministic fuzzy matcher cannot do.
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS)
        matches = sem.matching_tasks("grain storage north dakota", concept_embedder)
    assert matches
    assert matches[0].task == "Beet Piler - ND"
    assert isinstance(matches[0], SemanticMatch)
    # A coastal port query should instead surface the port.
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        harbor = sem.matching_tasks("harbor on the coast", concept_embedder)
    assert harbor[0].task == "Port of Long Beach"


def test_matches_are_sorted_by_descending_score(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS)
        matches = sem.matching_tasks("grain storage north dakota", concept_embedder, top_k=3)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_top_k_and_min_score_filter(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS)
        top1 = sem.matching_tasks("grain storage north dakota", concept_embedder, top_k=1)
        assert len(top1) == 1
        # A high threshold drops the unrelated tasks (their score is ~0).
        thresholded = sem.matching_tasks(
            "grain storage north dakota", concept_embedder, min_score=0.5
        )
        assert all(m.score >= 0.5 for m in thresholded)
        assert "Port of Long Beach" not in [m.task for m in thresholded]


def test_query_on_empty_index_raises(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        with pytest.raises(SemanticError, match="empty"):
            sem.matching_tasks("anything", concept_embedder)


def test_query_model_dimension_mismatch_raises(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS)
        # A query embedder producing a different-length vector is a model mismatch.
        with pytest.raises(SemanticError, match="different embedding model"):
            sem.matching_tasks("x", lambda texts: [[1.0, 2.0]])


def test_stats_reports_model_and_dim(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        sem.build(embedder=concept_embedder, task_names=_TASKS, model="concept-v1")
        s = sem.stats()
    assert s == {"tasks": 3, "model": "concept-v1", "dim": len(_CONCEPTS)}


# --- reading task names from a catalog index --------------------------------


def _catalog_with_tasks(tmp_path, tasks):
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        for i, task in enumerate(tasks):
            acq = f"2024-01-0{i + 1}-00-00-00_UMBRA-04"
            base = (
                "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
                f"sar-data/tasks/{task}/{acq}/{acq}"
            )
            doc = {
                "id": f"item-{i}",
                "properties": {"datetime": f"2024-01-0{i + 1}T00:00:00Z"},
                "bbox": [0, 0, 1, 1],
                "geometry": None,
                "assets": {},
            }
            idx.add(UmbraItem.from_dict(doc, href=f"{base}.stac.v2.json"))
        idx.commit()
    return path


def test_build_reads_task_names_from_index(tmp_path):
    catalog = _catalog_with_tasks(tmp_path, _TASKS)
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        written = sem.build(embedder=concept_embedder, index_path=catalog)
    assert written == len(_TASKS)


def test_build_missing_index_raises(tmp_path):
    with SemanticTaskIndex(tmp_path / "sem.db") as sem:
        with pytest.raises(SemanticError, match="No catalog index"):
            sem.build(embedder=concept_embedder, index_path=tmp_path / "nope.db")


def test_default_semantic_path_is_index_sibling(tmp_path):
    from umbra_py.semantic import default_semantic_path

    assert default_semantic_path(tmp_path / "catalog.db").name == "catalog.semantic.db"


# --- default_embedder configuration -----------------------------------------


def test_default_embedder_requires_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="OPENAI_API_KEY"):
        default_embedder()


def test_resolve_embed_model_precedence(monkeypatch):
    monkeypatch.delenv("UMBRA_EMBED_MODEL", raising=False)
    assert resolve_embed_model() == "text-embedding-3-small"
    assert resolve_embed_model("custom") == "custom"
    monkeypatch.setenv("UMBRA_EMBED_MODEL", "env-model")
    assert resolve_embed_model() == "env-model"
    assert resolve_embed_model("explicit") == "explicit"  # explicit wins over env


# --- CLI wiring (with an injected embedder, no network) ---------------------


@pytest.fixture
def _patched_embedder(monkeypatch):
    """Make the CLI use the deterministic concept embedder instead of a model."""
    monkeypatch.setattr(semantic_mod, "default_embedder", lambda *, model=None: concept_embedder)


def test_cli_build_then_search(tmp_path, monkeypatch, _patched_embedder):
    catalog = _catalog_with_tasks(tmp_path, _TASKS)
    sem_db = tmp_path / "sem.db"
    runner = CliRunner()

    built = runner.invoke(
        cli, ["semantic", "build", "--db", str(catalog), "--semantic-db", str(sem_db)]
    )
    assert built.exit_code == 0, built.output
    assert "Embedded 3" in built.output
    assert sem_db.exists()

    searched = runner.invoke(
        cli,
        ["semantic", "search", "grain storage north dakota", "--semantic-db", str(sem_db)],
    )
    assert searched.exit_code == 0, searched.output
    assert "Beet Piler - ND" in searched.output
    # It prints the deterministic command to run, for the user to audit.
    assert "umbra search --area" in searched.output


def test_cli_search_json_output(tmp_path, monkeypatch, _patched_embedder):
    catalog = _catalog_with_tasks(tmp_path, _TASKS)
    sem_db = tmp_path / "sem.db"
    runner = CliRunner()
    runner.invoke(cli, ["semantic", "build", "--db", str(catalog), "--semantic-db", str(sem_db)])

    result = runner.invoke(
        cli,
        [
            "semantic",
            "search",
            "grain storage north dakota",
            "--semantic-db",
            str(sem_db),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["query"] == "grain storage north dakota"
    assert payload["matches"][0]["task"] == "Beet Piler - ND"
    assert payload["matches"][0]["score"] >= payload["matches"][-1]["score"]


def test_cli_search_missing_index_errors(tmp_path, _patched_embedder):
    runner = CliRunner()
    result = runner.invoke(
        cli, ["semantic", "search", "anything", "--semantic-db", str(tmp_path / "nope.db")]
    )
    assert result.exit_code != 0
    assert "No semantic index" in result.output


def test_cli_info(tmp_path, monkeypatch, _patched_embedder):
    catalog = _catalog_with_tasks(tmp_path, _TASKS)
    sem_db = tmp_path / "sem.db"
    runner = CliRunner()
    runner.invoke(cli, ["semantic", "build", "--db", str(catalog), "--semantic-db", str(sem_db)])

    result = runner.invoke(cli, ["semantic", "info", "--semantic-db", str(sem_db)])
    assert result.exit_code == 0, result.output
    assert "tasks : 3" in result.output
    assert "dim   : 6" in result.output
