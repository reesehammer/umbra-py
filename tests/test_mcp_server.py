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
import sys
from pathlib import Path

import pytest
import responses

pytest.importorskip("mcp")

from mcp.server.mcpserver import Image  # noqa: E402

from umbra_py import mcp_server as ms  # noqa: E402
from umbra_py.exceptions import MissingDependencyError  # noqa: E402
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
        "stack_stats",
        "download_asset",
        "watch_site",
        "find_similar",
        "find_similar_text",
        "describe_scene",
        "narrate_change",
    }
    resources = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert resources == {"umbra://context", "umbra://index/stats"}
    prompts = {p.name for p in asyncio.run(server.list_prompts())}
    assert prompts == {
        "monitor-site",
        "watch-site",
        "quantify-change",
        "find-similar-scenes",
        "describe-scene",
        "narrate-change",
        "survey-region",
        "search-by-description",
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
# Canopy commercial-archive backend (configured via $UMBRA_CANOPY_TOKEN)
# --------------------------------------------------------------------------


def test_search_catalog_uses_canopy_archive_when_token_set(sample_item_dict, monkeypatch):
    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)

    class _FakeArchive:
        def __init__(self, *a, **k):
            _FakeArchive.token = k.get("token")

        def search(self, **kwargs):
            return iter([item])

    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    monkeypatch.setattr(ms, "UmbraCatalog", _FakeArchive)
    # A configured token routes the search to the commercial archive regardless
    # of any local index on the machine (local left unset).
    out = ms.search_catalog(area="anywhere", limit=5)

    assert out["source"] == "canopy-archive"
    assert out["count"] == 1
    assert out["items"][0]["id"] == sample_item_dict["id"]
    # The token is only ever handed to the catalog, never surfaced in the result.
    assert _FakeArchive.token == "secret-token"
    assert "secret-token" not in json.dumps(out)


def test_search_catalog_token_rejects_local_index(monkeypatch):
    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    # The archive is a live STAC API with no local index, so forcing local=True
    # while a token is configured is a deliberate, explained error.
    with pytest.raises(ValueError, match="commercial archive"):
        ms.search_catalog(area="anywhere", local=True)


def test_get_item_looks_up_archive_by_id_with_token(sample_item_dict, monkeypatch):
    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)

    class _FakeArchive:
        def __init__(self, *a, **k):
            self.token = k.get("token")

        def get_item(self, item_id):
            return item if item_id == item.id else None

    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    monkeypatch.setattr(ms, "UmbraCatalog", _FakeArchive)
    card = ms.get_item(item.id)
    assert card["id"] == sample_item_dict["id"]


def test_get_item_archive_missing_id_raises(monkeypatch):
    class _FakeArchive:
        def __init__(self, *a, **k):
            pass

        def get_item(self, item_id):
            return None

    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    monkeypatch.setattr(ms, "UmbraCatalog", _FakeArchive)
    with pytest.raises(ValueError, match="Canopy commercial archive"):
        ms.get_item("no-such-id")


@responses.activate
def test_get_item_reads_url_directly_even_with_token(sample_item_dict, monkeypatch):
    # With a token set, a bare id hits the archive, but a full URL is still read
    # directly as an open-data sidecar — the "://" escape hatch.
    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    card = ms.get_item(ITEM_URL)
    assert card["id"] == sample_item_dict["id"]


def test_build_server_instructions_mention_archive_with_token(monkeypatch):
    monkeypatch.setenv(ms.CANOPY_TOKEN_ENV, "secret-token")
    server = ms.build_server()
    assert "canopy-archive" in (server.instructions or "")


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
def test_image_tools_reach_the_client_as_image_blocks(sample_item_dict, monkeypatch):
    """Regression: the picture has to survive the *server*, not just the function.

    Every test above calls the tool functions directly, so nothing exercised the
    path a client actually takes. Through it, the SDK derives a structured-output
    schema from the ``list[Any]`` return annotation and serializes the result
    against it -- which cannot represent an ``Image``, so every call to the three
    image tools failed with ``ToolError: Unable to serialize unknown type``.
    They are registered without structured output for exactly this reason.
    """
    from PIL import Image as PILImage

    import umbra_py.viz as viz

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    monkeypatch.setattr(viz, "quicklook", lambda item, **kw: PILImage.new("RGB", (4, 4), (1, 2, 3)))

    server = ms.build_server()
    result = asyncio.run(server.call_tool("quicklook", {"url": ITEM_URL}))

    assert not result.is_error, result.content
    kinds = [type(block).__name__ for block in result.content]
    assert kinds == ["ImageContent", "TextContent"], kinds
    assert result.content[1].text.endswith(ms.ATTRIBUTION)


def test_json_tools_keep_structured_output():
    """The opt-out is scoped to the image tools: everything answering in JSON
    still advertises an output schema, which is what lets a client parse it."""
    server = ms.build_server()
    schemas = {t.name: t.output_schema for t in asyncio.run(server.list_tools())}

    for name in ("quicklook", "change_composite", "timescan"):
        assert schemas[name] is None, name
    for name in ("search_catalog", "get_item", "stack_stats"):
        assert schemas[name] is not None, name


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
def test_stack_stats_measures_the_series_in_numbers(sample_item_dict, monkeypatch):
    """The quantitative counterpart to the render tools: JSON, no model, no image."""
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")

    from umbra_py import load

    second_url = ITEM_URL.replace("item", "item2")
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    responses.add(responses.GET, second_url, json=sample_item_dict, status=200)

    seen: dict = {}

    def fake_to_stack(items, **kwargs):
        seen.update(kwargs)
        seen["items"] = list(items)
        return _constant_cube()

    monkeypatch.setattr(load, "to_stack", fake_to_stack)
    out = ms.stack_stats([ITEM_URL, second_url], bbox=[1.0, 2.0, 3.0, 4.0])

    # The agent surface asks for the projected grid and the decibel scale, so the
    # areas it reports are equal-area and the deltas are radiometric.
    assert seen["crs"] == "utm"
    assert seen["db"] is True
    assert seen["bbox"] == (1.0, 2.0, 3.0, 4.0)
    assert len(seen["items"]) == 2
    assert out["count"] == 2
    assert out["net_change"]["mean_delta_db"] == pytest.approx(6.0206, abs=0.01)
    assert out["net_change"]["changed_area_km2"] is not None
    assert out["attribution"] == ms.ATTRIBUTION
    assert out["caveats"]


@responses.activate
def test_stack_stats_blocks_add_the_spatial_breakdown(sample_item_dict, monkeypatch):
    """The agent asks *where* on the site by passing blocks=N; no model, no image."""
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")

    from umbra_py import load

    second_url = ITEM_URL.replace("item", "item2")
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    responses.add(responses.GET, second_url, json=sample_item_dict, status=200)
    monkeypatch.setattr(load, "to_stack", lambda items, **kwargs: _constant_cube())

    plain = ms.stack_stats([ITEM_URL, second_url])
    out = ms.stack_stats([ITEM_URL, second_url], blocks=2)

    assert "spatial" not in plain
    spatial = out["spatial"]
    assert (spatial["grid_rows"], len(spatial["blocks"])) == (2, 4)
    assert {b["compass"] for b in spatial["blocks"]} == {
        "northwest",
        "northeast",
        "southwest",
        "southeast",
    }
    # The cube brightens uniformly, so every block reports the same doubling and
    # each carries a lon/lat centre to locate it by.
    for block in spatial["blocks"]:
        assert block["net_change"]["mean_delta_db"] == pytest.approx(6.0206, abs=0.01)
        assert len(block["center_lonlat"]) == 2
    assert spatial["peak_block"]["direction"] == "brighter"


@responses.activate
def test_stack_stats_block_series_is_opt_in_on_the_agent_surface(sample_item_dict, monkeypatch):
    """An agent asks for the whole history only when the shape is the question."""
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")

    from umbra_py import load

    second_url = ITEM_URL.replace("item", "item2")
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    responses.add(responses.GET, second_url, json=sample_item_dict, status=200)
    monkeypatch.setattr(load, "to_stack", lambda items, **kwargs: _constant_cube())

    urls = [ITEM_URL, second_url]
    # Default off: the payload an agent gets for blocks=N is unchanged.
    assert all("series" not in b for b in ms.stack_stats(urls, blocks=2)["spatial"]["blocks"])

    blocks = ms.stack_stats(urls, blocks=2, block_series=True)["spatial"]["blocks"]
    for block in blocks:
        # Two passes -> the one interval the block's peak was picked from.
        assert block["series"] == [block["peak_interval"]]

    # A series with no grid to hang on is refused rather than silently dropped.
    with pytest.raises(ValueError, match="block_series needs a blocks grid"):
        ms.stack_stats(urls, block_series=True)


def _constant_cube():
    """A tiny two-pass dB cube on a projected grid, built without any I/O."""
    np = pytest.importorskip("numpy")
    xr = pytest.importorskip("xarray")

    values = np.stack([np.full((4, 4), 0.0), np.full((4, 4), 6.0206)]).astype("float32")
    return xr.DataArray(
        values,
        dims=("time", "y", "x"),
        coords={
            "time": np.array(
                ["2024-01-08T12:00:00", "2024-02-08T12:00:00"], dtype="datetime64[ns]"
            ),
            "y": [40.0, 30.0, 20.0, 10.0],
            "x": [0.0, 10.0, 20.0, 30.0],
            "item_id": ("time", ["a", "b"]),
        },
        attrs={
            "crs": "EPSG:32633",
            "transform": (10.0, 0.0, 0.0, 0.0, -10.0, 50.0),
            "bounds": (0.0, 0.0, 40.0, 50.0),
            "extent": "intersection",
            "units": "dB",
            "product_type": "GEC",
        },
    )


@responses.activate
def test_stack_stats_refuses_mixed_polarization_and_a_single_pass(sample_item_dict):
    vv_url = ITEM_URL
    hh_url = ITEM_URL.replace("item", "item2")
    responses.add(
        responses.GET, vv_url, json=_with_polarization(sample_item_dict, "VV"), status=200
    )
    responses.add(
        responses.GET, hh_url, json=_with_polarization(sample_item_dict, "HH"), status=200
    )
    with pytest.raises(ValueError, match="polarization"):
        ms.stack_stats([vv_url, hh_url])
    with pytest.raises(ValueError, match="at least two item URLs"):
        ms.stack_stats([vv_url])


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
# search_catalog semantic mode — task-name aliasing by meaning over MCP (C1)
#
# Fully offline: the query embedder is a deterministic bag-of-concepts stand-in
# (never a model call), so the whole describe-a-site path is exercised without an
# [ai] key or the network — the same boundary the CLI semantic tests hold.
# --------------------------------------------------------------------------

# A tiny concept embedder so "grain storage north dakota" lands near the label
# "Beet Piler - ND" (both touch the agriculture/storage/dakota concepts) while an
# unrelated label ("Port of Long Beach") does not — the aliasing a real embedder
# buys, made deterministic. Mirrors tests/test_semantic.py's concept embedder.
_SEM_CONCEPTS = ["agriculture", "storage", "dakota", "coast", "port"]
_SEM_WORDS = {
    "grain": ["agriculture"],
    "beet": ["agriculture"],
    "storage": ["storage"],
    "piler": ["storage", "agriculture"],
    "north": ["dakota"],
    "dakota": ["dakota"],
    "nd": ["dakota"],
    "port": ["port", "coast"],
    "beach": ["coast"],
}
_SEM_TOKENS = __import__("re").compile(r"[a-z0-9]+")


def _sem_vector(text: str) -> list[float]:
    weights = dict.fromkeys(_SEM_CONCEPTS, 0.0)
    for token in _SEM_TOKENS.findall(text.lower()):
        for concept in _SEM_WORDS.get(token, []):
            weights[concept] += 1.0
    return [weights[c] for c in _SEM_CONCEPTS]


def _concept_embedder(texts):
    return [_sem_vector(t) for t in texts]


def _build_semantic_index(path, tasks):
    from umbra_py import semantic as sem

    with sem.SemanticTaskIndex(path) as index:
        index.build(embedder=_concept_embedder, task_names=tasks, model="stub")


def _patch_semantic_index(monkeypatch, path):
    """Point the semantic layer at ``path`` and stub the (injectable) embedder."""
    from umbra_py import semantic as sem

    monkeypatch.setattr(sem, "default_semantic_path", lambda *a, **k: path)
    monkeypatch.setattr(sem, "default_embedder", lambda **k: _concept_embedder)


_SEM_TASKS = ["Beet Piler - ND", "Port of Long Beach"]


@responses.activate
def test_search_catalog_semantic_resolves_description_and_searches(
    sample_item_dict, monkeypatch, tmp_path
):
    path = tmp_path / "catalog.semantic.db"
    _build_semantic_index(path, _SEM_TASKS)
    _patch_semantic_index(monkeypatch, path)

    item = UmbraItem.from_dict(sample_item_dict, href=ITEM_URL)

    class _FakeCatalog:
        def search(self, **kwargs):
            _FakeCatalog.kwargs = kwargs
            return iter([item])

    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _FakeCatalog())

    out = ms.search_catalog(area="grain storage north dakota", semantic=True, local=False)

    # The description resolved by meaning to the agricultural-storage task, not
    # the coastal one it shares no concept with.
    assert out["resolved_area"] == "Beet Piler - ND"
    assert out["semantic_matches"][0]["task"] == "Beet Piler - ND"
    assert {m["task"] for m in out["semantic_matches"]} <= set(_SEM_TASKS)
    assert out["count"] == 1
    assert out["source"] == "live-catalog"
    # The resolved exact task name is searched literally (no fuzzy widening).
    assert _FakeCatalog.kwargs["area"] == "Beet Piler - ND"
    assert _FakeCatalog.kwargs["fuzzy"] is False


def test_search_catalog_semantic_no_match_returns_empty_audit_trail(monkeypatch, tmp_path):
    path = tmp_path / "catalog.semantic.db"
    _build_semantic_index(path, _SEM_TASKS)
    _patch_semantic_index(monkeypatch, path)

    # A query touching no shared concept has an all-zero vector (cosine 0.0 to
    # every task); a positive min_score then drops them all, so no unfiltered
    # catalog search runs and the (empty) audit trail is reported instead.
    out = ms.search_catalog(area="unmatched vocabulary", semantic=True, min_score=0.1)

    assert out["count"] == 0
    assert out["source"] == "semantic-index"
    assert out["resolved_area"] is None
    assert out["semantic_matches"] == []


def test_search_catalog_semantic_without_index_errors(monkeypatch, tmp_path):
    from umbra_py import semantic as sem

    missing = tmp_path / "missing.semantic.db"
    monkeypatch.setattr(sem, "default_semantic_path", lambda *a, **k: missing)
    with pytest.raises(FileNotFoundError, match="umbra semantic build"):
        ms.search_catalog(area="grain storage north dakota", semantic=True)


def test_search_catalog_semantic_requires_area(monkeypatch, tmp_path):
    path = tmp_path / "catalog.semantic.db"
    _build_semantic_index(path, _SEM_TASKS)
    _patch_semantic_index(monkeypatch, path)
    with pytest.raises(ValueError, match="needs `area`"):
        ms.search_catalog(semantic=True)


def test_search_catalog_semantic_and_fuzzy_mutually_exclusive():
    with pytest.raises(ValueError, match="not both"):
        ms.search_catalog(area="grain storage", semantic=True, fuzzy=True)


def test_search_catalog_semantic_missing_key_errors(monkeypatch, tmp_path):
    # With no stubbed embedder, default_embedder raises the [ai]-setup error, so
    # the tool never reaches a model implicitly.
    path = tmp_path / "catalog.semantic.db"
    _build_semantic_index(path, _SEM_TASKS)
    from umbra_py import semantic as sem

    monkeypatch.setattr(sem, "default_semantic_path", lambda *a, **k: path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError):
        ms.search_catalog(area="grain storage north dakota", semantic=True)


# --------------------------------------------------------------------------
# describe_scene — a SAR-literate VLM reading of one scene over MCP (C2)
#
# Fully offline: the describer (the model call) and the render are deterministic
# stand-ins, so the whole read-a-scene path is exercised without the [ai]/[viz]
# extras or the network — the same injectable boundary the CLI describe tests
# hold. This is the one MCP tool that consults a model; the stubs prove the
# deterministic edges (prompt build + parse boundary + provenance stamp) work
# without one.
# --------------------------------------------------------------------------


@responses.activate
def test_describe_scene_returns_validated_reading(sample_item_dict, monkeypatch):
    import umbra_py.describe  # noqa: F401  (ensure the submodule is imported)

    # ``from umbra_py import describe`` is the function; patch the real module's
    # globals (the render + the model call) via sys.modules, as test_describe does.
    dsc = sys.modules["umbra_py.describe"]

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    # Stand in for the render (no viz extra / no COG stream) and the model call
    # (no network / no key): the describer echoes a well-formed JSON reading.
    monkeypatch.setattr(dsc, "render_quicklook_png", lambda item, **kw: b"png-bytes")

    captured = {}

    def _fake_describer(*, model=None):
        def describer(messages):
            captured["messages"] = messages
            return json.dumps(
                {
                    "summary": "A bright grid of buildings beside a dark smooth river.",
                    "observed_features": ["bright building grid", "dark river"],
                    "confidence": "medium",
                    "caveats": ["the dark patch could be shadow, not water"],
                }
            )

        return describer

    monkeypatch.setattr(dsc, "default_describer", _fake_describer)

    out = ms.describe_scene(ITEM_URL)
    assert out["item_id"] == sample_item_dict["id"]
    assert out["summary"].startswith("A bright grid")
    assert out["observed_features"] == ["bright building grid", "dark river"]
    assert out["confidence"] == "medium"
    # Provenance and attribution are stamped deterministically, never by the model.
    assert out["attribution"]
    assert out["provenance"]
    # The model was shown the rendered picture and the metadata card, not asked to
    # invent them: the prompt carries the PNG bytes and the item's context.
    assert captured["messages"]["image_png"] == b"png-bytes"
    assert sample_item_dict["id"] in captured["messages"]["user"]


@responses.activate
def test_describe_scene_without_key_raises_setup_error(sample_item_dict, monkeypatch):
    import umbra_py.describe  # noqa: F401  (ensure the submodule is imported)

    dsc = sys.modules["umbra_py.describe"]

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    monkeypatch.setattr(dsc, "render_quicklook_png", lambda item, **kw: b"png-bytes")
    # No key configured -> the default describer refuses rather than running implicitly.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="vision model API key"):
        ms.describe_scene(ITEM_URL)


# --------------------------------------------------------------------------
# narrate_change — a number-grounded VLM reading of *change* over MCP (C2)
#
# The sibling of describe_scene: it composites two passes, computes the
# deterministic per-block dB grid, and has a model narrate only the change the
# numbers support. Fully offline — the render (which normally streams the COGs
# and needs the viz extra) and the model call are deterministic stand-ins, so
# the whole path (fetch → render+grid → prompt → parse → provenance stamp) runs
# with no [ai]/[viz] extra and no network.
# --------------------------------------------------------------------------


def _two_pass_urls(sample_item_dict):
    """Two same-polarization passes of one site, registered with ``responses``."""
    first_url = ITEM_URL
    second_url = ITEM_URL.replace("item", "item2")
    first = _with_polarization(sample_item_dict, "VV")
    second = _with_polarization(sample_item_dict, "VV")
    second["id"] = sample_item_dict["id"] + "-2"
    responses.add(responses.GET, first_url, json=first, status=200)
    responses.add(responses.GET, second_url, json=second, status=200)
    return first_url, second_url


@responses.activate
def test_narrate_change_returns_validated_narration(sample_item_dict, monkeypatch):
    import umbra_py.narrate  # noqa: F401  (ensure the submodule is imported)

    # ``from umbra_py import narrate`` is the function; patch the real module's
    # globals (the render + the model call) via sys.modules, as narrate.py's own
    # tests do, so the whole path runs with no viz extra and no key.
    nar = sys.modules["umbra_py.narrate"]

    first_url, second_url = _two_pass_urls(sample_item_dict)

    # Stand in for the render (no viz extra / no COG stream): return the composite
    # PNG bytes plus a deterministic dB-change grid the narration is grounded in.
    stats = nar.ChangeStats(
        grid_rows=2,
        grid_cols=2,
        change_threshold_db=3.0,
        bounds=(0.0, 0.0, 1.0, 1.0),
        blocks=[],
        scene_mean_abs_delta_db=4.2,
        scene_changed_fraction=0.3,
        peak_compass="northwest",
        peak_direction="brightened",
        peak_mean_delta_db=6.5,
    )
    monkeypatch.setattr(nar, "render_change_png", lambda items, **kw: (b"png-bytes", stats))

    captured = {}

    def _fake_narrator(*, model=None):
        def narrator(messages):
            captured["messages"] = messages
            return json.dumps(
                {
                    "summary": "The northwest corner brightened by several dB between passes.",
                    "changes": ["northwest block brightened ~6.5 dB"],
                    "confidence": "medium",
                    "caveats": ["one polarization only; speckle may inflate small blocks"],
                }
            )

        return narrator

    monkeypatch.setattr(nar, "default_narrator", _fake_narrator)

    out = ms.narrate_change([first_url, second_url])
    assert out["summary"].startswith("The northwest corner brightened")
    assert out["changes"] == ["northwest block brightened ~6.5 dB"]
    assert out["confidence"] == "medium"
    # The item ids of both passes are recorded deterministically.
    assert out["item_ids"] == [sample_item_dict["id"], sample_item_dict["id"] + "-2"]
    # The narration is grounded in — and carries — the deterministic dB grid.
    assert out["change_stats"]["peak_compass"] == "northwest"
    assert out["change_stats"]["scene_mean_abs_delta_db"] == 4.2
    # Provenance and attribution are stamped deterministically, never by the model.
    assert out["attribution"]
    assert out["provenance"]
    # The model was shown the rendered composite and the numeric grid, not asked
    # to invent them: the prompt carries the PNG bytes and the change numbers.
    assert captured["messages"]["image_png"] == b"png-bytes"
    assert "northwest" in captured["messages"]["user"]


@responses.activate
def test_narrate_change_refuses_mixed_polarization(sample_item_dict):
    vv_url = ITEM_URL
    hh_url = ITEM_URL.replace("item", "item2")
    responses.add(
        responses.GET, vv_url, json=_with_polarization(sample_item_dict, "VV"), status=200
    )
    responses.add(
        responses.GET, hh_url, json=_with_polarization(sample_item_dict, "HH"), status=200
    )
    # Mixed polarizations are not comparable, so change narration is refused before
    # any render or model call — the same guard change_composite holds.
    with pytest.raises(ValueError, match="polarization"):
        ms.narrate_change([vv_url, hh_url])


@responses.activate
def test_narrate_change_without_key_raises_setup_error(sample_item_dict, monkeypatch):
    import umbra_py.narrate  # noqa: F401  (ensure the submodule is imported)

    nar = sys.modules["umbra_py.narrate"]

    first_url, second_url = _two_pass_urls(sample_item_dict)
    # Render is stubbed, but no key is configured -> the default narrator refuses
    # rather than running implicitly.
    stats = nar.ChangeStats(
        grid_rows=1, grid_cols=1, change_threshold_db=3.0, bounds=(0.0, 0.0, 1.0, 1.0)
    )
    monkeypatch.setattr(nar, "render_change_png", lambda items, **kw: (b"png-bytes", stats))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(MissingDependencyError, match="vision model API key"):
        ms.narrate_change([first_url, second_url])
