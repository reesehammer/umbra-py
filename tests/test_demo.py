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
