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
    "find_repeat_sites",
    "get_item",
    "geocode_place",
    "index_stats",
    "stack_stats",
    "stack_provenance",
    "pick_change_interval",
    "download_asset",
    "watch_site",
    "find_similar",
    "find_similar_text",
    "describe_scene",
    "narrate_change",
    "stamp_description",
    "stamp_narration",
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
    # stack_stats is deterministic arithmetic returning JSON, so it needs no
    # native reimplementation the way the image tools do.
    assert li.stack_stats is ms.stack_stats
    # ...and so does its preflight, which reads raster headers and returns the
    # same report document the CLI and the HTTP endpoint emit.
    assert li.stack_provenance is ms.stack_provenance
    # find_repeat_sites is the deterministic discovery step that ranks the
    # repeat-imaged sites -- shared verbatim so find -> pick -> narrate is one chain.
    assert li.find_repeat_sites is ms.find_repeat_sites
    # pick_change_interval is the deterministic scan that names the pair worth
    # narrating -- shared verbatim so scan -> narrate is one inventory.
    assert li.pick_change_interval is ms.pick_change_interval
    # narrate_change is the C2 change-narration tool -- the last MCP tool to reach
    # the agent surfaces; it is the same callable here (no drift), not re-wrapped.
    assert li.narrate_change is ms.narrate_change
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
    assert li.ATTRIBUTION in out.content
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


# --------------------------------------------------------------------------
# narrate_change — the C2 number-grounded change reading, over LlamaIndex.
#
# It is the same MCP callable (no drift), the second opt-in tool that consults a
# model. The render and the model call are deterministic stand-ins, so the whole
# path runs through the FunctionTool wrapper with no [ai]/[viz] extra and no
# network; the validated narration dict rides on ToolOutput.raw_output.
# --------------------------------------------------------------------------


def _with_polarization(item_dict, pol):
    import copy

    d = copy.deepcopy(item_dict)
    d["properties"]["sar:polarizations"] = [pol]
    return d


@responses.activate
def test_narrate_change_tool_returns_validated_narration(sample_item_dict, monkeypatch):
    import sys

    import umbra_py.narrate  # noqa: F401  (ensure the submodule is imported)

    nar = sys.modules["umbra_py.narrate"]

    first_url = ITEM_URL
    second_url = ITEM_URL.replace("item", "item2")
    first = _with_polarization(sample_item_dict, "VV")
    second = _with_polarization(sample_item_dict, "VV")
    second["id"] = sample_item_dict["id"] + "-2"
    responses.add(responses.GET, first_url, json=first, status=200)
    responses.add(responses.GET, second_url, json=second, status=200)

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

    def _fake_narrator(*, model=None):
        def narrator(messages):
            return json.dumps(
                {
                    "summary": "The northwest corner brightened by several dB between passes.",
                    "changes": ["northwest block brightened ~6.5 dB"],
                    "confidence": "medium",
                    "caveats": ["one polarization only"],
                }
            )

        return narrator

    monkeypatch.setattr(nar, "default_narrator", _fake_narrator)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    narrate = _tool(li.umbra_tools(), "narrate_change")
    out = narrate.call(urls=[first_url, second_url]).raw_output

    assert out["summary"].startswith("The northwest corner brightened")
    assert out["item_ids"] == [first["id"], second["id"]]
    # The narration carries the deterministic dB grid and the provenance stamp.
    assert out["change_stats"]["peak_compass"] == "northwest"
    assert out["attribution"]
    assert out["provenance"]


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

    # Called directly, the shared guard refuses mixing HH and VV (not comparable).
    with pytest.raises(ValueError, match="polarization"):
        li.narrate_change([vv_url, hh_url])
