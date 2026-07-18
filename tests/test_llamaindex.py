"""Tests for the LlamaIndex tool adapter (``umbra_py.llamaindex``).

The whole module is skipped when the ``llamaindex`` extra is not installed, so
the core CI job (which installs only ``[dev]``) never sees it; the all-extras job
installs ``[dev,all,mcp,serve,ai,langchain,llamaindex]`` and runs it. Everything
here is offline: network is mocked with ``responses`` and the renderers are
patched, so no live catalog access is required and the suite stays deterministic.

The design contract under test is *no drift*: the JSON tools are the very same
callables the MCP server exposes, and the render tools are native
reimplementations (so the LlamaIndex surface never pulls in the MCP SDK) that
return the PNG on a ``RenderResult`` — surfaced as the ``ToolOutput.raw_output``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

pytest.importorskip("llama_index.core")

from umbra_py import llamaindex as li  # noqa: E402
from umbra_py import mcp_server as ms  # noqa: E402
from umbra_py.models import UmbraItem  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
ITEM_URL = "https://umbra-open-data-catalog.s3.amazonaws.com/x/item.stac.v2.json"
_NOMINATIM = "https://nominatim.openstreetmap.org/search"

_EXPECTED_NAMES = {
    "search_catalog",
    "get_item",
    "geocode_place",
    "index_stats",
    "download_asset",
    "watch_site",
    "find_similar",
    "find_similar_text",
    "describe_scene",
    "quicklook",
    "change_composite",
    "timescan",
}
_RENDER_NAMES = {"quicklook", "change_composite", "timescan"}


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())


def _tool(tools, name):
    return next(t for t in tools if t.metadata.name == name)


# --------------------------------------------------------------------------
# Toolkit assembly
# --------------------------------------------------------------------------


def test_umbra_tools_registers_expected_surface():
    tools = li.umbra_tools()
    assert {t.metadata.name for t in tools} == _EXPECTED_NAMES


def test_umbra_tools_json_only_drops_render_tools():
    tools = li.umbra_tools(include_render=False)
    assert {t.metadata.name for t in tools} == _EXPECTED_NAMES - _RENDER_NAMES


def test_tool_descriptions_and_schema_are_inferred():
    tools = li.umbra_tools()
    search = _tool(tools, "search_catalog")
    # The docstring becomes the tool description an agent's model reads.
    assert search.metadata.description and "Search Umbra's catalog" in search.metadata.description
    # The args schema is inferred from the function signature.
    props = search.metadata.get_parameters_dict()["properties"]
    for arg in ("bbox", "place", "start", "end", "products", "limit"):
        assert arg in props


def test_json_tools_are_the_same_callables_as_mcp():
    # No drift: the JSON tools reuse the MCP server's deterministic callables
    # verbatim (single source of truth), while the render tools are native
    # reimplementations so this surface never imports the MCP SDK.
    assert li.search_catalog is ms.search_catalog
    assert li.get_item is ms.get_item
    assert li.watch_site is ms.watch_site
    assert li.quicklook is not ms.quicklook
    assert li.change_composite is not ms.change_composite


# --------------------------------------------------------------------------
# JSON tool invocation (end-to-end through the FunctionTool wrapper)
# --------------------------------------------------------------------------


@responses.activate
def test_search_catalog_tool_invocation(sample_item_dict, monkeypatch):
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

    search = _tool(li.umbra_tools(), "search_catalog")
    # Calling through the FunctionTool exercises schema coercion + dispatch; the
    # deterministic dict rides on ToolOutput.raw_output.
    out = search.call(place="Somewhere", limit=5, local=False)

    assert out.raw_output["source"] == "live-catalog"
    assert out.raw_output["count"] == 1
    assert out.raw_output["items"][0]["id"] == sample_item_dict["id"]
    assert _FakeCatalog.kwargs["bbox"] == (-68.0, 10.0, -67.0, 11.0)


@responses.activate
def test_get_item_tool_invocation(sample_item_dict):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    get_item = _tool(li.umbra_tools(), "get_item")
    card = get_item.call(url=ITEM_URL).raw_output
    assert card["id"] == sample_item_dict["id"]
    assert card["attribution"]


def test_index_stats_tool_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(ms, "default_index_path", lambda: tmp_path / "missing.db")
    stats = _tool(li.umbra_tools(), "index_stats")
    assert stats.call().raw_output["available"] is False


# --------------------------------------------------------------------------
# Render tools — "images are the API" via RenderResult (caption + PNG)
# --------------------------------------------------------------------------


@responses.activate
def test_quicklook_tool_returns_png_render_result(sample_item_dict, monkeypatch):
    from PIL import Image as PILImage

    import umbra_py.viz as viz

    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    monkeypatch.setattr(viz, "quicklook", lambda item, **kw: PILImage.new("RGB", (4, 4), (1, 2, 3)))

    quicklook = _tool(li.umbra_tools(), "quicklook")
    out = quicklook.call(url=ITEM_URL)
    # The caption is the human-readable content; the PNG bytes ride on raw_output.
    assert out.content.endswith(li.ATTRIBUTION)
    result = out.raw_output
    assert isinstance(result, li.RenderResult)
    assert str(result) == out.content
    assert isinstance(result.png, bytes)
    assert result.png.startswith(b"\x89PNG")


@responses.activate
def test_change_composite_tool_refuses_mixed_polarization(sample_item_dict):
    import copy

    vv_url = ITEM_URL
    hh_url = ITEM_URL.replace("item", "item2")

    def _with_pol(pol):
        d = copy.deepcopy(sample_item_dict)
        d["properties"]["sar:polarizations"] = [pol]
        return d

    responses.add(responses.GET, vv_url, json=_with_pol("VV"), status=200)
    responses.add(responses.GET, hh_url, json=_with_pol("HH"), status=200)

    # Called directly, the guard raises (mixing HH and VV is not comparable).
    with pytest.raises(ValueError, match="polarization"):
        li.change_composite([vv_url, hh_url])


@responses.activate
def test_change_composite_needs_two_urls(sample_item_dict):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    with pytest.raises(ValueError, match="at least two"):
        li.change_composite([ITEM_URL])


@responses.activate
def test_timescan_needs_two_urls(sample_item_dict):
    responses.add(responses.GET, ITEM_URL, json=sample_item_dict, status=200)
    with pytest.raises(ValueError, match="at least two"):
        li.timescan([ITEM_URL])
