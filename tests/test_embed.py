"""Offline tests for archive scene embeddings (:mod:`umbra_py.embed`).

No test calls a model or touches the network: both the render step and the
embedding step are injectable callables, so a deterministic stand-in stands in for
each. The stand-in renderer emits a short "scene token" per item (instead of
streaming a real quicklook), and the stand-in embedder maps that token onto a
small vocabulary of visual concepts, so two "flood" scenes land close in vector
space while a "city" scene does not -- exactly the visual similarity a real CLIP
embedder buys, made deterministic. Everything the module does with those vectors
(rendering dispatch, storage, cosine ranking, thresholding, the CLI wiring) is
what is actually under test.
"""

from __future__ import annotations

import sqlite3
import sys

import pytest
from click.testing import CliRunner

from umbra_py.cli import cli
from umbra_py.embed import (
    EmbedError,
    SceneEmbeddingIndex,
    SceneMatch,
    default_image_embedder,
    default_scene_embed_path,
    default_text_embedder,
    resolve_scene_model,
)
from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem

# ``from umbra_py import embed`` is a submodule; grab the real module object for
# monkeypatching its globals (as test_describe does for its submodule).
embed_mod = sys.modules["umbra_py.embed"]


# --- A deterministic stand-in renderer + embedder ---------------------------

# Each scene "looks like" a bag of visual concepts. Two acquisitions of the same
# kind (two floods) share concepts, so their vectors point the same way; an
# unrelated kind (a city) does not.
_CONCEPTS = ["water", "field", "urban", "port"]
_SCENE_CONCEPTS: dict[str, list[str]] = {
    "flood": ["water", "field"],
    "farmland": ["field"],
    "city": ["urban"],
    "harbor": ["water", "port"],
}
# Text words map onto the same concepts, so text-to-scene search shares the space.
_WORD_CONCEPTS: dict[str, list[str]] = {
    "flooded": ["water", "field"],
    "flood": ["water", "field"],
    "water": ["water"],
    "field": ["field"],
    "farm": ["field"],
    "city": ["urban"],
    "urban": ["urban"],
    "buildings": ["urban"],
    "harbor": ["water", "port"],
    "port": ["port"],
    "ships": ["port"],
    "berth": ["port"],
}


def _concept_vector(tokens: list[str], table: dict[str, list[str]]) -> list[float]:
    weights = dict.fromkeys(_CONCEPTS, 0.0)
    for token in tokens:
        for concept in table.get(token, []):
            weights[concept] += 1.0
    return [weights[c] for c in _CONCEPTS]


def scene_renderer(item: UmbraItem) -> bytes:
    """A stand-in :data:`~umbra_py.embed.Renderer`: encode the item's scene kind
    (read from a marker in its id) as bytes, instead of rendering a real COG."""
    kind = item.id.split("-", 1)[0]
    return f"scene:{kind}".encode()


def image_embedder(images: list[bytes]) -> list[list[float]]:
    """Map each rendered scene token to its concept vector (one per image)."""
    out = []
    for png in images:
        kind = png.decode().split(":", 1)[1]
        # The scene kind maps straight to its concepts (an image "is" its concepts).
        out.append(_concept_vector([kind], {kind: _SCENE_CONCEPTS.get(kind, [])}))
    return out


def text_embedder(texts: list[str]) -> list[list[float]]:
    """Map a text query into the same concept space (a joint model stand-in)."""
    import re

    return [_concept_vector(re.findall(r"[a-z]+", t.lower()), _WORD_CONCEPTS) for t in texts]


def _item(scene_kind: str, n: int) -> UmbraItem:
    """Build an UmbraItem whose id encodes its scene kind, with a realistic href
    (so task/datetime/href populate the stored record)."""
    item_id = f"{scene_kind}-{n}"
    task = f"Task-{scene_kind}"
    base = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        f"sar-data/tasks/{task}/2024-01-0{n}-00-00-00_UMBRA-04/scene"
    )
    doc = {
        "id": item_id,
        "properties": {"datetime": f"2024-01-0{n}T00:00:00Z"},
        "bbox": [0, 0, 1, 1],
        "geometry": None,
        "assets": {},
    }
    return UmbraItem.from_dict(doc, href=f"{base}.stac.v2.json")


_ITEMS = [
    _item("flood", 1),
    _item("flood", 2),
    _item("city", 3),
    _item("harbor", 4),
    _item("farmland", 5),
]


def _build(path, items=_ITEMS, model="default"):
    with SceneEmbeddingIndex(path) as idx:
        written = idx.build(items, embedder=image_embedder, render=scene_renderer, model=model)
    return written


# --- build + storage round-trip ---------------------------------------------


def test_build_embeds_and_persists(tmp_path):
    written = _build(tmp_path / "e.db")
    assert written == len(_ITEMS)
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        assert len(idx) == len(_ITEMS)
        assert "flood-1" in idx
        assert "not-there" not in idx


def test_build_records_context_fields(tmp_path):
    _build(tmp_path / "e.db")
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        # similar() carries task/datetime/href straight from the stored row.
        matches = idx.similar(image_embedder([b"scene:flood"])[0], top_k=1)
    assert matches[0].task == "Task-flood"
    assert matches[0].datetime == "2024-01-01T00:00:00+00:00"
    assert matches[0].href and matches[0].href.endswith(".stac.v2.json")


def test_build_is_idempotent(tmp_path):
    path = tmp_path / "e.db"
    assert _build(path) == len(_ITEMS)
    # Re-running embeds nothing new; only a new acquisition is embedded.
    assert _build(path) == 0
    assert _build(path, items=[*_ITEMS, _item("city", 6)]) == 1
    with SceneEmbeddingIndex(path) as idx:
        assert len(idx) == len(_ITEMS) + 1


def test_build_dedupes_within_a_batch(tmp_path):
    path = tmp_path / "e.db"
    dup = _item("flood", 1)
    assert _build(path, items=[dup, dup, _item("city", 3)]) == 2


def test_build_survives_reopen(tmp_path):
    path = tmp_path / "e.db"
    _build(path)
    with SceneEmbeddingIndex(path) as idx:
        assert len(idx) == len(_ITEMS)
        assert idx.stored_model() == "default"


def test_build_rejects_mixed_models(tmp_path):
    path = tmp_path / "e.db"
    _build(path, model="model-a")
    with SceneEmbeddingIndex(path) as idx:
        with pytest.raises(EmbedError, match="refusing to mix"):
            idx.build(
                [_item("city", 6)], embedder=image_embedder, render=scene_renderer, model="model-b"
            )


def test_build_rejects_bad_embedder_shape(tmp_path):
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        with pytest.raises(EmbedError, match="vectors for"):
            idx.build(
                [_item("flood", 1), _item("city", 3)],
                embedder=lambda imgs: [[1.0, 2.0]],
                render=scene_renderer,
            )


def test_build_skips_render_errors_by_default(tmp_path):
    def flaky_render(item: UmbraItem) -> bytes:
        if item.id.startswith("city"):
            raise RuntimeError("COG failed to stream")
        return scene_renderer(item)

    seen: list[str] = []
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        written = idx.build(
            _ITEMS,
            embedder=image_embedder,
            render=flaky_render,
            on_error=lambda it, exc: seen.append(it.id),
        )
    assert written == len(_ITEMS) - 1  # the city scene was skipped
    assert seen == ["city-3"]


def test_build_can_raise_on_render_errors(tmp_path):
    def boom(item: UmbraItem) -> bytes:
        raise RuntimeError("nope")

    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        with pytest.raises(EmbedError, match="Could not render"):
            idx.build(
                [_item("flood", 1)], embedder=image_embedder, render=boom, skip_render_errors=False
            )


def test_schema_version_is_stamped(tmp_path):
    path = tmp_path / "e.db"
    with SceneEmbeddingIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == embed_mod._SCHEMA_VERSION
    finally:
        conn.close()


def test_future_schema_version_is_rejected(tmp_path):
    path = tmp_path / "e.db"
    with SceneEmbeddingIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 999")
    conn.commit()
    conn.close()
    with pytest.raises(EmbedError, match="schema version 999"):
        SceneEmbeddingIndex(path)


# --- similarity: the point of the feature -----------------------------------


def test_similar_to_item_ranks_the_alike_scene_first(tmp_path):
    _build(tmp_path / "e.db")
    query = _item("flood", 9)  # a fresh flood scene, not in the index
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        matches = idx.similar_to_item(query, embedder=image_embedder, render=scene_renderer)
    assert matches
    assert isinstance(matches[0], SceneMatch)
    # The other flood scenes rank above the city / farmland / harbor scenes.
    assert matches[0].item_id in {"flood-1", "flood-2"}
    assert matches[1].item_id in {"flood-1", "flood-2"}
    assert matches[0].score >= matches[-1].score


def test_similar_to_item_excludes_itself(tmp_path):
    _build(tmp_path / "e.db")
    query = _ITEMS[0]  # flood-1, already indexed
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        matches = idx.similar_to_item(query, embedder=image_embedder, render=scene_renderer)
    ids = [m.item_id for m in matches]
    assert "flood-1" not in ids  # a scene never returns itself
    assert "flood-2" in ids


def test_similar_is_sorted_by_descending_score(tmp_path):
    _build(tmp_path / "e.db")
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        matches = idx.similar(image_embedder([b"scene:flood"])[0], top_k=5)
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_top_k_and_min_score_filter(tmp_path):
    _build(tmp_path / "e.db")
    fvec = image_embedder([b"scene:flood"])[0]
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        assert len(idx.similar(fvec, top_k=1)) == 1
        thresholded = idx.similar(fvec, min_score=0.5)
    assert all(m.score >= 0.5 for m in thresholded)
    assert "city-3" not in [m.item_id for m in thresholded]  # unrelated scene dropped


def test_text_to_scene_search(tmp_path):
    _build(tmp_path / "e.db")
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        floods = idx.similar_to_text("a flooded field", text_embedder)
        harbors = idx.similar_to_text("ships at a port berth", text_embedder)
    assert floods[0].item_id in {"flood-1", "flood-2"}
    assert harbors[0].item_id == "harbor-4"


def test_query_on_empty_index_raises(tmp_path):
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        with pytest.raises(EmbedError, match="empty"):
            idx.similar([1.0, 0.0, 0.0, 0.0])


def test_query_model_dimension_mismatch_raises(tmp_path):
    _build(tmp_path / "e.db")
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        with pytest.raises(EmbedError, match="different embedding model"):
            idx.similar([1.0, 2.0])  # wrong length -> model mismatch


def test_stats_reports_model_and_dim(tmp_path):
    _build(tmp_path / "e.db", model="clip-v1")
    with SceneEmbeddingIndex(tmp_path / "e.db") as idx:
        s = idx.stats()
    assert s == {"scenes": len(_ITEMS), "model": "clip-v1", "dim": len(_CONCEPTS)}


def test_scene_match_to_dict_rounds_score():
    m = SceneMatch(item_id="x", score=0.123456789, task="T", datetime="d", href="h")
    d = m.to_dict()
    assert d["item_id"] == "x"
    assert d["score"] == round(0.123456789, 6)
    assert d["task"] == "T"


# --- default embedder configuration -----------------------------------------


def test_default_image_embedder_requires_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="OPENAI_API_KEY"):
        default_image_embedder()


def test_default_text_embedder_requires_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="OPENAI_API_KEY"):
        default_text_embedder()


def test_resolve_scene_model_precedence(monkeypatch):
    monkeypatch.delenv("UMBRA_SCENE_EMBED_MODEL", raising=False)
    assert resolve_scene_model() == "clip"
    assert resolve_scene_model("custom") == "custom"
    monkeypatch.setenv("UMBRA_SCENE_EMBED_MODEL", "env-model")
    assert resolve_scene_model() == "env-model"
    assert resolve_scene_model("explicit") == "explicit"  # explicit wins over env


def test_default_image_embedder_sends_data_uri(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    seen = {}

    def fake_post(url, headers, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    monkeypatch.setattr(embed_mod, "_post_json", fake_post)
    vecs = default_image_embedder(model="clip")([b"\x89PNGfake"])
    assert vecs == [[0.1, 0.2]]
    assert seen["url"] == "https://proxy.example/v1/embeddings"
    assert seen["payload"]["model"] == "clip"
    assert seen["payload"]["input"][0].startswith("data:image/png;base64,")


def test_default_scene_embed_path_is_index_sibling(tmp_path):
    assert default_scene_embed_path(tmp_path / "catalog.db").name == "catalog.embed.db"


# --- CLI wiring (with injected renderer + embedder, no network) -------------


@pytest.fixture
def _patched(monkeypatch):
    """Point the CLI's default embedders + renderer at the deterministic stand-ins
    and its network fetch at fixed items, so no test touches a model or S3."""
    monkeypatch.setattr(embed_mod, "default_image_embedder", lambda **k: image_embedder)
    monkeypatch.setattr(embed_mod, "default_text_embedder", lambda **k: text_embedder)
    monkeypatch.setattr(embed_mod, "_render_quicklook_asset", lambda it, **k: scene_renderer(it))


def test_cli_build_then_similar(tmp_path, monkeypatch, _patched):
    edb = tmp_path / "e.db"
    runner = CliRunner()

    urls = [i.href for i in _ITEMS]
    items_by_url = {i.href: i.raw for i in _ITEMS}
    monkeypatch.setattr("umbra_py.cli.get_json", lambda url: items_by_url[url])

    built = runner.invoke(cli, ["embed", "build", *urls, "--embed-db", str(edb)])
    assert built.exit_code == 0, built.output
    assert "Embedded 5" in built.output
    assert edb.exists()

    # A fresh flood scene should surface the indexed floods.
    query = _item("flood", 9)
    monkeypatch.setattr("umbra_py.cli.get_json", lambda url: query.raw)
    sim = runner.invoke(cli, ["embed", "similar", query.href, "--embed-db", str(edb)])
    assert sim.exit_code == 0, sim.output
    assert "flood-1" in sim.output or "flood-2" in sim.output


def test_cli_similar_json_output(tmp_path, monkeypatch, _patched):
    import json

    edb = tmp_path / "e.db"
    _build(edb)
    query = _item("flood", 9)
    monkeypatch.setattr("umbra_py.cli.get_json", lambda url: query.raw)
    result = CliRunner().invoke(
        cli, ["embed", "similar", query.href, "--embed-db", str(edb), "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["query"] == "flood-9"
    assert data["matches"]
    assert data["matches"][0]["item_id"] in {"flood-1", "flood-2"}
    assert "score" in data["matches"][0]


def test_cli_search_text(tmp_path, monkeypatch, _patched):
    edb = tmp_path / "e.db"
    _build(edb)
    result = CliRunner().invoke(cli, ["embed", "search", "a flooded field", "--embed-db", str(edb)])
    assert result.exit_code == 0, result.output
    assert "flood-1" in result.output or "flood-2" in result.output


def test_cli_info(tmp_path, monkeypatch, _patched):
    edb = tmp_path / "e.db"
    _build(edb, model="clip-v1")
    result = CliRunner().invoke(cli, ["embed", "info", "--embed-db", str(edb)])
    assert result.exit_code == 0, result.output
    assert "scenes : 5" in result.output
    assert "clip-v1" in result.output


def test_cli_similar_without_index_errors(tmp_path):
    result = CliRunner().invoke(
        cli, ["embed", "similar", "https://x/item.json", "--embed-db", str(tmp_path / "no.db")]
    )
    assert result.exit_code != 0
    assert "Build one first" in result.output


def test_cli_build_rejects_urls_and_search(tmp_path):
    result = CliRunner().invoke(cli, ["embed", "build", "https://x/item.json", "--area", "Foo"])
    assert result.exit_code != 0
    assert "OR search criteria" in result.output


# --- fetching a published scene index ---------------------------------------


def test_fetch_prebuilt_embeddings_downloads_and_opens(tmp_path):
    """from_release() / fetch_prebuilt_embeddings() pull the published sidecar and
    open a working, queryable index -- no rebuild, no model call on the fetch."""
    import responses

    from umbra_py.embed import fetch_prebuilt_embeddings

    # A real, populated embed DB serialized to bytes stands in for the asset the
    # publish workflow would upload to the catalog-index release.
    src = tmp_path / "published.embed.db"
    _build(src, model="clip-v1")
    payload = src.read_bytes()

    url = "https://example.com/catalog-index/catalog.embed.db"
    dest = tmp_path / "fetched" / "catalog.embed.db"

    @responses.activate
    def run_fetch():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        returned = fetch_prebuilt_embeddings(dest, url=url)
        assert returned == dest
        with SceneEmbeddingIndex.from_release(dest, url=url) as idx:
            return idx.stats(), idx.similar(_concept_vector(["water", "field"], _WORD_CONCEPTS))

    stats, matches = run_fetch()
    assert dest.exists()
    assert stats["scenes"] == 5
    assert stats["model"] == "clip-v1"
    # The fetched vectors are usable immediately: a flood-shaped query ranks the
    # indexed floods first.
    assert matches[0].item_id in {"flood-1", "flood-2"}


def test_fetch_prebuilt_embeddings_overwrites_existing(tmp_path):
    """A re-fetch replaces an older sidecar at the same path."""
    import responses

    from umbra_py.embed import fetch_prebuilt_embeddings

    dest = tmp_path / "catalog.embed.db"
    dest.write_bytes(b"stale-not-a-db")

    fresh = tmp_path / "fresh.embed.db"
    _build(fresh, items=[_item("flood", 1)], model="clip-v1")
    payload = fresh.read_bytes()

    url = "https://example.com/catalog.embed.db"

    @responses.activate
    def run_fetch():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        fetch_prebuilt_embeddings(dest, url=url)
        with SceneEmbeddingIndex(dest) as idx:
            return idx.stats()

    assert run_fetch()["scenes"] == 1


def test_cli_embed_fetch(tmp_path, monkeypatch):
    """`umbra embed fetch` downloads the published sidecar and reports it."""
    import responses

    src = tmp_path / "published.embed.db"
    _build(src, model="clip-v1")
    payload = src.read_bytes()

    url = "https://example.com/catalog.embed.db"
    dest = tmp_path / "fetched.embed.db"

    @responses.activate
    def run_cli():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        return CliRunner().invoke(cli, ["embed", "fetch", "--embed-db", str(dest), "--url", url])

    result = run_cli()
    assert result.exit_code == 0, result.output
    assert "Fetched scene index: 5" in result.output
    assert "clip-v1" in result.output
    assert dest.exists()
