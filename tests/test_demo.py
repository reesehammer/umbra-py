"""Tests for the ``umbra demo`` self-serve interactive explorer.

Like ``test_lazy_imagery``, these exercise the *Python* side of the contract:
the generated page carries the right data, controls, and driver wiring. The
JavaScript runs in a browser and isn't reachable from pytest, so we stop at "the
page ships the right things". The generator is stdlib-only, so none of this
needs the viz extra.
"""

from __future__ import annotations

import json
import re

from umbra_py import demo
from umbra_py.models import UmbraItem

_HREF = "https://x.s3.amazonaws.com/sar-data/tasks/Centerfield, Utah/t1/a1/item.stac.v2.json"


def _config(html: str) -> dict:
    """Pull the embedded ``window.UMBRA_DEMO`` JSON back out of the page."""
    m = re.search(r"window\.UMBRA_DEMO = (\{.*?\});", html)
    assert m, "no embedded config found"
    # Undo the </-neutralisation the generator applies.
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_build_demo_embeds_config_and_controls(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], title="Explorer", subtitle="Centerfield")

    # The three faceted filter controls the demo-gap doc says are missing today.
    assert 'id="umbra-text"' in html  # free-text site/id search
    assert 'id="umbra-start"' in html and 'id="umbra-end"' in html  # date range
    assert 'id="umbra-products"' in html  # product-type chips
    # Clustering (the scale answer past Folium's polygon ceiling).
    assert "markerClusterGroup" in html
    # Mandatory attribution travels with the data.
    assert "CC BY 4.0" in html

    cfg = _config(html)
    assert cfg["title"] == "Explorer"
    assert cfg["subtitle"] == "Centerfield"
    assert len(cfg["features"]) == 1
    props = cfg["features"][0]["properties"]
    assert props["id"] == item.id
    assert props["product"] == "GEC"
    assert props["centroid"] is not None


def test_build_demo_derives_product_and_date_facets(sample_item_dict):
    """The product chips and the date-range bounds are derived from the data,
    so an empty facet never appears and the sliders frame the real extent."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    cfg = _config(demo.build_demo([item]))
    assert cfg["products"] == ["GEC"]
    # sample_item is acquired 2024-01-01.
    assert cfg["dateMin"] == "2024-01-01"
    assert cfg["dateMax"] == "2024-01-01"


def test_build_demo_lazy_imagery_wires_the_shared_driver(sample_item_dict):
    """With lazy imagery on, the page must ship the geotiff.js driver and each
    feature must carry the COG URL + placement bounds the button needs."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], lazy_imagery=True)

    assert "umbraToggleSarImage" in html  # the shared driver
    assert "umbraLazyMap" in html  # the non-Folium map-resolution hook
    cfg = _config(html)
    props = cfg["features"][0]["properties"]
    assert props["lazy_url"] and props["lazy_url"].startswith("http")
    # data-bounds order is [south, west, north, east].
    s, w, n, e = props["lazy_bounds"]
    assert s < n and w < e
    assert cfg["lazyImagery"] is True


def test_build_demo_metadata_only_omits_driver(sample_item_dict):
    """``lazy_imagery=False`` builds a metadata-only explorer: no geotiff.js
    driver installed (so no CDN dependency), and the config says so."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], lazy_imagery=False)

    cfg = _config(html)
    assert cfg["lazyImagery"] is False
    # The driver (which *defines* umbraToggleSarImage) must be absent; the app
    # JS only ever *references* window.umbraToggleSarImage behind a guard.
    assert "window.umbraToggleSarImage = function" not in html
    assert "GeoTIFF.fromUrl" not in html


def test_build_demo_static_by_default_no_server(sample_item_dict):
    """With no ``server_url`` the page stays fully static: the config carries no
    server URL and the analyze panel is hidden."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item])
    cfg = _config(html)
    assert cfg["serverUrl"] is None
    assert 'id="umbra-analyze" style="display:none"' in html


def test_build_demo_server_url_wires_analysis_panel(sample_item_dict):
    """With ``server_url`` set the config carries it and the app JS POSTs the
    filtered view to the server's artifact endpoints (R4 wiring)."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], server_url="http://localhost:8000/")
    cfg = _config(html)
    # Stored verbatim; the trailing slash is trimmed client-side.
    assert cfg["serverUrl"] == "http://localhost:8000/"
    assert "/artifacts/' + spec.kind" in html
    # The three products are wired.
    for btn in ("umbra-btn-change", "umbra-btn-timescan", "umbra-btn-swipe"):
        assert btn in html


def test_build_demo_server_url_wires_thumbnail_preview(sample_item_dict):
    """With ``server_url`` set the detail panel leads with the baked-thumbnail
    endpoint (DEMO_APP_GAPS G6): the app JS builds an ``.umbra-thumb`` image from
    ``/artifacts/thumbnail/<id>.png`` off the server base, url-encoding the
    remote item id, and drops it on error so an unbaked scene is never a broken
    image."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], server_url="http://localhost:8000/")
    # The preview reuses the one server base (also used by the analyze panel).
    assert "serverBase" in html
    # It targets the baked-thumbnail endpoint, url-encoding the remote id (the
    # builder is shared with the whole-archive PMTiles explorer).
    assert "/artifacts/thumbnail/' + encodeURIComponent(id) + '.png'" in html
    assert "window.umbraThumb(serverBase, p.id)" in html
    # The image class exists in the stylesheet, and the element is dropped on a
    # 404 (a scene with no baked thumbnail) rather than shown broken.
    assert ".umbra-thumb" in html
    assert "thumb.onerror" in html


def test_build_demo_no_thumbnail_preview_without_server(sample_item_dict):
    """Without ``server_url`` the detail panel stays metadata-only: there is no
    server base to build a thumbnail URL from, so the preview never fires and the
    page is fully static."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item])
    cfg = _config(html)
    assert cfg["serverUrl"] is None
    # The wiring string ships in the static app JS, but it is guarded by
    # ``serverBase`` (null here), so no request is ever made.
    assert "serverBase = CFG.serverUrl" in html


def test_build_demo_drops_unmappable_items(sample_item_dict):
    """An item with neither footprint nor bbox can't be placed or clustered, so
    it must be dropped rather than emitted as a null marker."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    ghost = UmbraItem(id="no-geo")  # no geometry, no bbox
    cfg = _config(demo.build_demo([item, ghost]))
    ids = [f["properties"]["id"] for f in cfg["features"]]
    assert item.id in ids
    assert "no-geo" not in ids


def test_build_demo_pins_cdn_versions():
    """A drifting CDN URL silently breaks a generated page. Leaflet and the
    marker-cluster plugin must be pinned, like the geotiff.js dep."""
    assert re.search(r"leaflet@\d+\.\d+", demo.LEAFLET_JS)
    assert re.search(r"markercluster@\d+\.\d+", demo.MARKERCLUSTER_JS)


def test_build_demo_neutralises_script_breakout():
    """A place name containing ``</script>`` must not break out of the embedded
    JSON data block."""
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    item.properties["umbra:task_id"] = "</script><script>alert(1)</script>"
    html = demo.build_demo([item])
    # The raw closing tag must not appear inside the data block; it's escaped to
    # "<\/script>". The config still round-trips.
    assert "</script><script>alert(1)" not in html
    cfg = _config(html)
    assert len(cfg["features"]) == 1


def test_save_demo_writes_html(tmp_path, sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    out = demo.save_demo([item], tmp_path / "explorer.html", subtitle="s")
    assert out.exists()
    text = out.read_text()
    assert text.lstrip().startswith("<!DOCTYPE html>")
    assert "umbra-map" in text


def test_title_is_html_escaped(sample_item_dict):
    """The title reaches the ``<title>`` element, so a stray ``<`` must be
    escaped there (the JSON copy is separately safe via json.dumps)."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item], title="A & <b>")
    assert "<title>A &amp; &lt;b&gt;</title>" in html


# --- shared lazy-imagery driver: the non-Folium map fallback ---------------


def test_lazy_driver_falls_back_to_global_map():
    """The demo page is not a Folium page, so the shared driver must resolve the
    map via the ``window.umbraLazyMap`` fallback when the DOM walk finds no
    ``.folium-map`` ancestor -- without disturbing the Folium path."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "window.umbraLazyMap" in js
    # The Folium DOM-walk resolution is still the primary path.
    assert "folium-map" in js


# --- CLI --------------------------------------------------------------------


def test_cli_demo_writes_html(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([item]))

    out = tmp_path / "demo.html"
    result = CliRunner().invoke(cli_mod.cli, ["demo", "--area", "Center", "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "window.UMBRA_DEMO" in text
    assert "markerClusterGroup" in text


def test_cli_demo_rejects_non_html(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([]))
    result = CliRunner().invoke(
        cli_mod.cli, ["demo", "--area", "X", "--out", str(tmp_path / "x.geojson")]
    )
    assert result.exit_code != 0
    assert "html" in result.output.lower()


def test_cli_demo_no_results(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([]))
    result = CliRunner().invoke(
        cli_mod.cli, ["demo", "--area", "X", "--out", str(tmp_path / "x.html")]
    )
    assert result.exit_code != 0
    assert "no items" in result.output.lower()


def test_cli_demo_no_lazy_imagery_flag(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([item]))

    out = tmp_path / "demo.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["demo", "--area", "X", "--no-lazy-imagery", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert '"lazyImagery":false' in text.replace(" ", "")


def test_build_demo_drops_javascript_stac_href():
    """A ``javascript:`` STAC href must not reach the client as a clickable
    link (the front end assigns ``stac_href`` to an anchor's ``href``)."""
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0), href="javascript:alert(1)")
    html = demo.build_demo([item])
    assert "javascript:alert" not in html
    cfg = _config(html)
    assert cfg["features"][0]["properties"]["stac_href"] is None


def test_build_demo_keeps_http_stac_href():
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0), href="https://example/item.json")
    cfg = _config(demo.build_demo([item]))
    assert cfg["features"][0]["properties"]["stac_href"] == "https://example/item.json"


def test_demo_feature_prefers_baked_place_label():
    """When the index has baked a place label, the explorer shows it over the
    task codename; without one it falls back to the task."""
    labelled = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0), href=_HREF)
    labelled.place = "Reykjavik, Iceland"
    feature = demo._demo_feature(labelled, {})
    assert feature["properties"]["place"] == "Reykjavik, Iceland"

    unlabelled = UmbraItem(id="y", bbox=(0.0, 0.0, 1.0, 1.0), href=_HREF)
    fallback = demo._demo_feature(unlabelled, {})
    assert fallback["properties"]["place"] == unlabelled.task


# --- whole-archive mode over PMTiles ----------------------------------------


def test_build_demo_pmtiles_mode_reads_the_tiled_archive():
    """With ``pmtiles_url`` the page becomes a MapLibre vector map over the
    archive: the archive URL and source-layer travel in the config, the pinned
    MapLibre + PMTiles bundles replace Leaflet, and nothing is embedded."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")

    cfg = _config(html)
    assert cfg["pmtilesUrl"] == "catalog.pmtiles"
    assert cfg["pmtilesLayer"] == "acquisitions"
    # The whole point: the page carries no item slice at all.
    assert "features" not in cfg

    assert "pmtiles://" in html
    assert "maplibre-gl" in html
    # ...and not the embedded-slice stack.
    assert "markerClusterGroup" not in html
    assert "leaflet.js" not in html
    # Attribution still rides along, as on every other visual artifact.
    assert "CC BY 4.0" in html


def test_build_demo_pmtiles_mode_ignores_items():
    """The archive is the data source, so handing items in must not embed them
    -- the page has to stay the same size for a catalog of any size."""
    items = [UmbraItem(id=f"i{n}", bbox=(0.0, 0.0, 1.0, 1.0), href=_HREF) for n in range(50)]
    with_items = demo.build_demo(items, pmtiles_url="catalog.pmtiles")
    without = demo.build_demo([], pmtiles_url="catalog.pmtiles")
    assert with_items == without
    assert "i49" not in with_items


def test_build_demo_pmtiles_mode_keeps_the_sidebar_contract():
    """Same explorer, different data source: the sidebar controls the slice app
    wires are all still present and driven."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")
    for element_id in ("umbra-text", "umbra-start", "umbra-end", "umbra-products", "umbra-reset"):
        assert f'id="{element_id}"' in html
    # The filters are pushed into the tiles as MapLibre expressions rather than
    # evaluated over an in-page array.
    assert "setFilter" in html
    assert "index-of" in html


def test_build_demo_pmtiles_mode_offers_the_closed_product_set():
    """Facets cannot be derived from a slice the page never reads, so the chips
    are the known product set and the date range starts unbounded (framing a
    sample's extent would hide most of the archive behind a default filter)."""
    from umbra_py.constants import PRODUCT_ASSETS

    cfg = _config(demo.build_demo([], pmtiles_url="catalog.pmtiles"))
    assert cfg["products"] == list(PRODUCT_ASSETS)
    assert "dateMin" not in cfg and "dateMax" not in cfg


def test_build_demo_pmtiles_mode_wires_the_server_panels():
    """A server-backed whole-archive page keeps both `umbra serve` affordances:
    the thumbnail preview (G6) and the "Analyze this view" panel (R4)."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles", server_url="http://localhost:8000/")
    cfg = _config(html)
    assert cfg["serverUrl"] == "http://localhost:8000/"
    assert "umbraAnalyzePanel" in html
    assert "/artifacts/thumbnail/" in html

    static = demo.build_demo([], pmtiles_url="catalog.pmtiles")
    assert _config(static)["serverUrl"] is None


def test_build_demo_pmtiles_mode_draws_the_footprint_polygons():
    """The archive carries footprint polygons at the deeper zooms, so the page
    draws coverage shape -- filtered with, and clickable like, the markers."""
    from umbra_py.pmtiles import FOOTPRINT_LAYER

    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")
    assert _config(html)["pmtilesFootprintLayer"] == FOOTPRINT_LAYER
    # A fill (the click target) and an outline layer over the footprint layer.
    assert "umbra-footprint-fill" in html
    assert "umbra-footprint-line" in html
    assert "'fill'" in html and "'line'" in html
    # One filter expression drives markers and outlines together: a scene hidden
    # by the sidebar must not leave its footprint drawn.
    assert "[LAYER_ID, FILL_ID, OUTLINE_ID].forEach" in html
    # Clicking the polygon opens the same detail panel as clicking the centroid.
    assert "[LAYER_ID, FILL_ID].forEach" in html


def test_build_demo_pmtiles_layer_is_configurable():
    cfg = _config(demo.build_demo([], pmtiles_url="c.pmtiles", pmtiles_layer="scenes"))
    assert cfg["pmtilesLayer"] == "scenes"


def test_both_modes_share_one_analyze_panel(sample_item_dict):
    """The detail-row builder, the thumbnail preview and the analysis panel are
    map-engine agnostic, so both explorers must ship the *same* code rather than
    each carrying a copy that could drift."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    slice_page = demo.build_demo([item], server_url="http://s")
    archive_page = demo.build_demo([], pmtiles_url="c.pmtiles", server_url="http://s")
    for helper in ("window.umbraRow", "window.umbraThumb", "window.umbraAnalyzePanel"):
        assert helper in slice_page
        assert helper in archive_page
    assert demo._SHARED_JS in slice_page
    assert demo._SHARED_JS in archive_page


def test_build_demo_pmtiles_mode_offers_the_sar_overlay():
    """The last thing the embedded-slice explorer had over the whole-archive
    one: clicking a scene in the tiled archive must offer the same on-click
    "Get SAR image" overlay, driven by the same geotiff.js driver."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")

    assert _config(html)["lazyImagery"] is True
    # The shared button builder, wired to the driver's toggle contract.
    assert "window.umbraSarButton(p.id, cogUrl(p), p.bounds)" in html
    assert "window.umbraToggleSarImage" in html
    # ...and the driver itself is shipped, keyed by the geotiff.js CDN pin.
    from umbra_py._lazy_imagery import GEOTIFF_JS

    assert GEOTIFF_JS in html


def test_build_demo_pmtiles_mode_places_the_overlay_with_maplibre():
    """MapLibre has no ``imageOverlay``; the page must ship the image-source
    build of the driver, and publish the map the driver falls back to."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")

    assert "window.umbraLazyMap = map" in html
    assert "type: 'image'" in html and "'raster-fade-duration'" in html
    # The Leaflet placement must not leak onto a page with no Leaflet loaded.
    assert "L.imageOverlay" not in html
    # The overlay slots under the acquisition layers so they stay clickable.
    assert "window.umbraOverlayBeforeId = FILL_ID" in html


def test_build_demo_pmtiles_mode_rebuilds_the_cog_url_safely():
    """``cog`` is a basename resolved against ``stac_href``; both come from
    remote metadata and end up in a fetch(), so only http(s) may survive."""
    html = demo.build_demo([], pmtiles_url="catalog.pmtiles")
    assert "function cogUrl(p)" in html
    # An absolute reference is used as-is, a relative one joined to the sidecar
    # directory -- and anything that is not http(s) resolves to null.
    assert r"if (/^https?:\/\//.test(cog)) return cog;" in html
    # A non-http(s) sidecar href, or a `cog` with a path in it, resolves to null
    # rather than to a rebuilt-wrong (or javascript:) URL.
    assert r"if (!/^https?:\/\//.test(href) || cog.indexOf('/') !== -1) return null;" in html


def test_build_demo_pmtiles_metadata_only_drops_the_overlay():
    """``lazy_imagery=False`` must reach the whole-archive page too: no driver,
    no geotiff.js CDN dependency at click time."""
    from umbra_py._lazy_imagery import GEOTIFF_JS

    html = demo.build_demo([], pmtiles_url="catalog.pmtiles", lazy_imagery=False)
    assert _config(html)["lazyImagery"] is False
    assert GEOTIFF_JS not in html


def test_both_modes_share_one_sar_button_builder(sample_item_dict):
    """Same button, same driver contract, one implementation -- only the
    placement differs, and that lives in the driver."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    slice_page = demo.build_demo([item])
    archive_page = demo.build_demo([], pmtiles_url="c.pmtiles")
    for page in (slice_page, archive_page):
        assert "window.umbraSarButton = function" in page


def test_cli_demo_pmtiles_honours_no_lazy_imagery(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    out = tmp_path / "demo.html"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["demo", "--pmtiles", "c.pmtiles", "--no-lazy-imagery", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert '"lazyImagery":false' in out.read_text().replace(" ", "")


def test_build_demo_default_mode_is_unchanged(sample_item_dict):
    """No ``pmtiles_url`` must leave the proven embedded-slice page alone."""
    item = UmbraItem.from_dict(sample_item_dict, href=_HREF)
    html = demo.build_demo([item])
    assert "markerClusterGroup" in html
    assert "maplibre" not in html
    assert "pmtiles" not in html
    assert len(_config(html)["features"]) == 1


def test_cli_demo_pmtiles_builds_without_searching(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    def _no_search(self, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("--pmtiles must not walk the catalog")

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", _no_search)

    out = tmp_path / "demo.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["demo", "--pmtiles", "catalog.pmtiles", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "whole-archive explorer" in result.output
    assert "pmtiles://" in out.read_text()


def test_cli_demo_pmtiles_refuses_search_options(tmp_path):
    """Search flags would be gathered and thrown away, so they are an error
    rather than a silently unfiltered page."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli,
        ["demo", "--pmtiles", "c.pmtiles", "--area", "Center", "--out", str(tmp_path / "d.html")],
    )
    assert result.exit_code != 0
    assert "--area" in result.output
