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
    }
    resources = {str(r.uri) for r in asyncio.run(server.list_resources())}
    assert resources == {"umbra://context", "umbra://index/stats"}
    prompts = {p.name for p in asyncio.run(server.list_prompts())}
    assert prompts == {"monitor-site", "survey-region"}


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
