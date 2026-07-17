"""Tests for the umbra-mcp MCP server (``umbra_py.mcp_server``).

The whole module is skipped when the ``mcp`` extra is not installed, so the
core CI job (which installs only ``[dev]``) never sees it; the all-extras job
installs ``[dev,all,mcp]`` and runs it. Everything here is offline: network is
mocked with ``responses`` and the renderers are patched, so no live catalog
access is required and the suite stays deterministic.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import responses

pytest.importorskip("mcp")

from mcp.server.fastmcp import Image  # noqa: E402

from umbra_py import mcp_server as ms  # noqa: E402
from umbra_py.models import UmbraItem  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
ITEM_URL = "https://umbra-open-data-catalog.s3.amazonaws.com/x/item.stac.v2.json"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())


def _with_polarization(base: dict, pol: str) -> dict:
    item = copy.deepcopy(base)
    item["properties"]["sar:polarizations"] = [pol]
    return item


# --------------------------------------------------------------------------
# Server assembly
# --------------------------------------------------------------------------


def test_build_server_registers_expected_surface():
    server = ms.build_server()
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert tools == {
        "search_catalog",
        "get_item",
        "geocode_place",
        "index_stats",
        "quicklook",
        "change_composite",
        "timescan",
        "download_asset",
        "watch_site",
        "find_similar",
        "find_similar_text",
        "describe_scene",
    }
    resources = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert resources == {"umbra://context", "umbra://index/stats"}
    prompts = {p.name for p in asyncio.run(server.list_prompts())}
    assert prompts == {
        "monitor-site",
        "watch-site",
        "find-similar-scenes",
        "describe-scene",
        "survey-region",
    }


def test_context_resource_matches_llm_context():
    from umbra_py import llm_context

    server = ms.build_server()
    content = asyncio.run(server.read_resource("umbra://context"))
    payload = json.loads(list(content)[0].content)
    assert payload == llm_context()


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@responses.activate
def test_get_item_returns_context_card(sample_item_dict):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    card = ms.get_item(ITEM_URL)
    assert card["id"] == sample_item_dict["id"]
    # The card carries the change-detection caveat and the license line — the
    # things a model needs that a human already knows.
    assert "polarization_caveat" in card
    assert card["attribution"]


@responses.activate
def test_geocode_place_tool_returns_bbox(sample_item_dict):
    responses.add(
        responses.GET,
        _NOMINATIM,
        json=[{"boundingbox": ["32.5", "42.0", "-124.4", "-114.1"], "display_name": "California"}],
        status=200,
    )
    out = ms.geocode_place("California")
    assert out["bbox"] == [-124.4, 32.5, -114.1, 42.0]
    assert out["display_name"] == "California"


@responses.activate
def test_search_catalog_geocodes_place_and_returns_cards(sample_item_dict, monkeypatch):
    responses.add(
        responses.GET,
        _NOMINATIM,
        json=[{"boundingbox": ["10.0", "11.0", "-68.0", "-67.0"], "display_name": "Somewhere"}],
        status=200,
    )
    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)

    class _FakeCatalog:
        def search(self, **kwargs):
            _FakeCatalog.kwargs = kwargs
            return iter([item])

    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _FakeCatalog())
    # Force the live path regardless of any local index on the machine.
    out = ms.search_catalog(place="Somewhere", limit=5, local=False)

    assert out["source"] == "live-catalog"
    assert out["count"] == 1
    assert out["resolved_place"] == "Somewhere"
    assert out["resolved_bbox"] == [-68.0, 10.0, -67.0, 11.0]
    assert out["items"][0]["id"] == sample_item_dict["id"]
    # The geocoded bbox is passed through to the deterministic search layer.
    assert _FakeCatalog.kwargs["bbox"] == (-68.0, 10.0, -67.0, 11.0)
    assert _FakeCatalog.kwargs["limit"] == 5


def test_search_catalog_local_without_index_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "missing.db")
    with pytest.raises(FileNotFoundError):
        ms.search_catalog(area="anywhere", local=True)


def test_index_stats_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "missing.db")
    out = ms.index_stats()
    assert out["available"] is False
    assert "umbra index fetch" in out["hint"]


# --------------------------------------------------------------------------
# Image tools + deterministic guards
# --------------------------------------------------------------------------


@responses.activate
def test_quicklook_returns_image_block(sample_item_dict, monkeypatch):
    from PIL import Image as PILImage

    import umbra_py.viz as viz

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    monkeypatch.setattr(viz, "quicklook", lambda item, **kw: PILImage.new("RGB", (4, 4), (1, 2, 3)))
    out = ms.quicklook(ITEM_URL)
    assert isinstance(out[0], Image)
    assert out[1].endswith(ms.ATTRIBUTION)  # caption carries the attribution line


@responses.activate
def test_change_composite_refuses_mixed_polarization(sample_item_dict):
    vv_url = ITEM_URL
    hh_url = ITEM_URL.replace("item", "item2")
    responses.add(
        responses.GET, vv_url, json=_with_polarization(sample_item_dict, "VV"), status=200
    )
    responses.add(
        responses.GET, hh_url, json=_with_polarization(sample_item_dict, "HH"), status=200
    )
    with pytest.raises(ValueError, match="polarization"):
        ms.change_composite([vv_url, hh_url])


@responses.activate
def test_download_asset_confirm_gate(sample_item_dict):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    href = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL).asset_href("GEC")
    responses.add(responses.HEAD, href, headers={"Content-Length": "2500000"}, status=200)
    out = ms.download_asset(ITEM_URL, "GEC", confirm=False)
    assert out["confirm_required"] is True
    assert out["bytes"] == 2500000
    assert "confirm=true" in out["hint"]


# --------------------------------------------------------------------------
# watch_site — the standing-analyst delta, over MCP
# --------------------------------------------------------------------------


def _fake_catalog(items):
    """A minimal live-catalog stand-in returning ``items`` from ``search``."""

    class _FakeCatalog:
        def search(self, **kwargs):
            return iter(list(items))

    return _FakeCatalog()


def test_watch_site_reports_only_new_items(sample_item_dict, monkeypatch, tmp_path):
    # State persists in the local index at the default path; point it at a temp
    # dir so the test never touches a real user index.
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "catalog.db")

    first = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)
    second_dict = copy.deepcopy(sample_item_dict)
    second_dict["id"] = sample_item_dict["id"] + "-2"
    second_url = ITEM_URL.replace("item", "item2")
    second = UmbraItem.from_dict(second_dict, href=second_url)

    # First check: live source has one pass; it is new (first run reports all).
    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _fake_catalog([first]))
    out1 = ms.watch_site(area="anywhere", local=False)
    assert out1["source"] == "live-catalog"
    assert out1["first_run"] is True
    assert out1["new_count"] == 1
    assert out1["new_items"][0]["id"] == first.id
    assert out1["attribution"]

    # Second check, same query: the source now has both passes, but only the
    # second is new — the first was already reported and state persisted.
    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _fake_catalog([first, second]))
    out2 = ms.watch_site(area="anywhere", local=False)
    assert out2["first_run"] is False
    assert out2["new_count"] == 1
    assert out2["new_items"][0]["id"] == second.id
    assert out2["total_seen"] == 2

    # Idempotent: an immediate re-run with no newly published data reports zero.
    out3 = ms.watch_site(area="anywhere", local=False)
    assert out3["new_count"] == 0
    assert out3["total_seen"] == 2


def test_watch_site_reset_reestablishes_baseline(sample_item_dict, monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "catalog.db")
    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)
    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _fake_catalog([item]))

    ms.watch_site(area="anywhere", local=False)
    # Without reset the same pass is not new again...
    assert ms.watch_site(area="anywhere", local=False)["new_count"] == 0
    # ...but reset re-reports it as a fresh baseline.
    out = ms.watch_site(area="anywhere", local=False, reset=True)
    assert out["first_run"] is True
    assert out["new_count"] == 1


@responses.activate
def test_watch_site_geocodes_place(sample_item_dict, monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "catalog.db")
    responses.add(
        responses.GET,
        _NOMINATIM,
        json=[{"boundingbox": ["10.0", "11.0", "-68.0", "-67.0"], "display_name": "Somewhere"}],
        status=200,
    )
    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)

    captured = {}

    class _FakeCatalog:
        def search(self, **kwargs):
            captured.update(kwargs)
            return iter([item])

    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _FakeCatalog())
    out = ms.watch_site(place="Somewhere", local=False)
    assert out["resolved_place"] == "Somewhere"
    assert out["resolved_bbox"] == [-68.0, 10.0, -67.0, 11.0]
    # The geocoded bbox reaches the deterministic search layer.
    assert captured["bbox"] == (-68.0, 10.0, -67.0, 11.0)


# --------------------------------------------------------------------------
# find_similar / find_similar_text — visual similarity search over MCP (C5)
#
# Fully offline: the embedder and renderer are deterministic stand-ins (never a
# model call), so the whole scene-embedding path is exercised without the
# [ai]/[viz] extras or the network — the same boundary the CLI embed tests hold.
# --------------------------------------------------------------------------


def _stub_image_embedder(images):
    """Deterministic image embedder: a 3-vector derived from the PNG bytes, so
    identical inputs map to identical vectors and distinct ones stay distinct."""
    return [[float(len(b)), float(sum(b) % 100), 1.0] for b in images]


def _build_scene_index(path, items):
    """Build a scene-embedding index at ``path`` from ``items`` with the stub
    embedder and a render that stands in for the quicklook (item id as bytes)."""
    from umbra_py import embed as emb

    with emb.SceneEmbeddingIndex(path) as index:
        index.build(
            items,
            embedder=_stub_image_embedder,
            render=lambda it: it.id.encode(),
            model="stub",
        )


def _patch_scene_index(monkeypatch, path):
    """Point the embed layer at ``path`` and stub out the model/render calls."""
    from umbra_py import embed as emb

    monkeypatch.setattr(emb, "default_scene_embed_path", lambda *a, **k: path)
    monkeypatch.setattr(emb, "default_image_embedder", lambda **k: _stub_image_embedder)
    monkeypatch.setattr(emb, "default_text_embedder", lambda **k: lambda q: [[7.0, 3.0, 1.0]])
    monkeypatch.setattr(emb, "_render_quicklook_asset", lambda it, **k: it.id.encode())


@responses.activate
def test_find_similar_returns_matches_and_excludes_self(sample_item_dict, monkeypatch, tmp_path):
    path = tmp_path / "catalog.embed.db"
    first = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)
    second_dict = copy.deepcopy(sample_item_dict)
    second_dict["id"] = sample_item_dict["id"] + "-2"
    second_url = ITEM_URL.replace("item", "item2")
    second = UmbraItem.from_dict(second_dict, href=second_url)
    _build_scene_index(path, [first, second])

    _patch_scene_index(monkeypatch, path)
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)

    out = ms.find_similar(ITEM_URL)
    assert out["query"] == {"kind": "image", "item_id": first.id, "asset": "GEC"}
    assert out["model"] == "stub"
    assert out["attribution"]
    # The query item is excluded from its own results; only the other scene ranks.
    ids = [m["item_id"] for m in out["matches"]]
    assert ids == [second.id]
    assert out["matches"][0]["href"] == second_url  # hand-off pointer for get_item/quicklook


def test_find_similar_without_index_errors(monkeypatch, tmp_path):
    from umbra_py import embed as emb

    missing = tmp_path / "missing.embed.db"
    monkeypatch.setattr(emb, "default_scene_embed_path", lambda *a, **k: missing)
    with pytest.raises(FileNotFoundError, match="umbra embed build"):
        ms.find_similar(ITEM_URL)


def test_find_similar_text_ranks_stored_scenes(sample_item_dict, monkeypatch, tmp_path):
    path = tmp_path / "catalog.embed.db"
    first = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)
    second_dict = copy.deepcopy(sample_item_dict)
    second_dict["id"] = sample_item_dict["id"] + "-2"
    second = UmbraItem.from_dict(second_dict, href=ITEM_URL.replace("item", "item2"))
    _build_scene_index(path, [first, second])

    _patch_scene_index(monkeypatch, path)
    out = ms.find_similar_text("ships at a berth")
    assert out["query"] == {"kind": "text", "text": "ships at a berth"}
    assert out["count"] == 2  # text query has no self to exclude; ranks every stored scene
    assert out["model"] == "stub"
    assert {m["item_id"] for m in out["matches"]} == {first.id, second.id}


def test_find_similar_text_without_index_errors(monkeypatch, tmp_path):
    from umbra_py import embed as emb

    missing = tmp_path / "missing.embed.db"
    monkeypatch.setattr(emb, "default_scene_embed_path", lambda *a, **k: missing)
    with pytest.raises(FileNotFoundError, match="umbra embed build"):
        ms.find_similar_text("a flooded field")


# --------------------------------------------------------------------------
# describe_scene — the VLM scene reading over MCP (C2)
#
# Fully offline: the model step (default_describer) and the render step
# (render_quicklook_png) are injectable module globals on umbra_py.describe, so
# these exercise the whole path — fetch → render → prompt → parse → provenance —
# without an [ai] key, the [viz] extra, or the network, the same interpretation
# boundary the CLI describe tests hold.
# --------------------------------------------------------------------------

# ``from umbra_py import describe`` (the function) shadows the module attribute on
# the package, so fetch the real submodule from sys.modules to patch its globals.
import sys  # noqa: E402

_describe_mod = sys.modules["umbra_py.describe"]


def _fake_describer(reply):
    """A describer that ignores the multimodal prompt and returns a fixed reply."""
    return lambda messages: reply


@responses.activate
def test_describe_scene_returns_structured_reading(sample_item_dict, monkeypatch):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    reply = json.dumps(
        {
            "summary": "A dark smooth river curves through a bright built-up area.",
            "observed_features": ["bright grid of buildings in the northeast"],
            "confidence": "medium",
            "caveats": ["the dark band could be calm water or radar shadow"],
        }
    )
    # Stub both edges: the model reply and the quicklook render (no network/viz).
    monkeypatch.setattr(_describe_mod, "default_describer", lambda **k: _fake_describer(reply))
    monkeypatch.setattr(_describe_mod, "render_quicklook_png", lambda item, **k: b"PNG")

    out = ms.describe_scene(ITEM_URL)
    assert out["item_id"] == sample_item_dict["id"]
    assert out["summary"].startswith("A dark smooth river")
    assert out["observed_features"] == ["bright grid of buildings in the northeast"]
    assert out["confidence"] == "medium"
    assert out["caveats"] == ["the dark band could be calm water or radar shadow"]
    assert out["asset"] == "GEC"
    # Provenance and attribution are stamped deterministically, never by the model.
    assert out["attribution"] == ms.ATTRIBUTION
    assert out["provenance"]


@responses.activate
def test_describe_scene_without_key_errors(sample_item_dict, monkeypatch):
    from umbra_py.exceptions import MissingDependencyError

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    # Render is stubbed so the failure is specifically the missing model key,
    # not the [viz] extra; clear any provider key the environment might carry.
    monkeypatch.setattr(_describe_mod, "render_quicklook_png", lambda item, **k: b"PNG")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="API key"):
        ms.describe_scene(ITEM_URL)
