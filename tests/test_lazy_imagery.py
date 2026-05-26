"""Tests for the browser-side lazy SAR imagery overlay.

These exercise the Python side of the contract -- the rendered HTML
contains the right markers, the right URLs, and the right driver. The
JS itself (georaster-layer-for-leaflet + geotiff.js) runs in a browser
and isn't reachable from pytest, so we deliberately stop at "the page
asks for the right things".
"""

from __future__ import annotations

import re

import pytest

from umbra_py.models import UmbraItem


def test_popup_button_html_carries_id_and_url():
    """Each per-item button has to carry both the item id (so the
    driver can dedupe layers) and the asset URL (so the click handler
    can stream the COG without a server round-trip)."""
    from umbra_py._lazy_imagery import popup_button_html

    out = popup_button_html(
        item_id="abc-123",
        asset_url="https://example.com/scene.tif",
    )
    assert 'data-item-id="abc-123"' in out
    assert 'data-asset-url="https://example.com/scene.tif"' in out
    assert 'onclick="umbraToggleSarImage(this)"' in out
    # Default state must be idle so the driver's toggle works.
    assert 'data-state="idle"' in out


def test_popup_button_html_escapes_attacker_controlled_attrs():
    """The asset URL ultimately comes from a STAC document we don't
    own. Don't let a crafted href escape the attribute and inject
    script into the page."""
    from umbra_py._lazy_imagery import popup_button_html

    out = popup_button_html(
        item_id='evil" onclick="alert(1)',
        asset_url='https://example.com/"><script>x()</script>',
    )
    # The literal quote must be escaped so the attribute boundary
    # holds. We don't care which escape style HTML uses (numeric vs
    # named), just that no raw closing quote leaks through and no
    # second executable handler ends up on the element.
    assert '"><script>' not in out
    assert 'onclick="alert(1)' not in out
    # Only the legitimate handler should appear with an opening quote
    # (the attacker's `onclick=` got escaped into `onclick=&quot;` so
    # the browser sees it as part of data-item-id, not an attribute).
    assert out.count('onclick="') == 1
    assert 'onclick="umbraToggleSarImage(this)"' in out


def test_cdn_script_tags_pin_versions():
    """A drifting CDN URL silently breaks browser-side decoding. The
    deps must be pinned so a release reproduces."""
    from umbra_py._lazy_imagery import cdn_script_tags

    tags = cdn_script_tags()
    assert "georaster" in tags
    assert "georaster-layer-for-leaflet" in tags
    # Both URLs must carry an explicit @<version> segment.
    assert re.search(r"georaster@\d+\.\d+", tags), tags
    assert re.search(r"georaster-layer-for-leaflet@\d+\.\d+", tags), tags


def test_driver_script_references_map_var():
    """The driver looks the Folium map up by its (random) JS variable
    name; if the substitution gets dropped, every click silently does
    nothing."""
    from umbra_py._lazy_imagery import driver_script

    js = driver_script(
        map_var="map_abc123",
        percentile_low=2.0,
        percentile_high=98.0,
        sample_cap=100_000,
    )
    assert "window['map_abc123']" in js
    assert "umbraToggleSarImage" in js
    # Both percentile cuts must reach the percentile() call sites.
    assert "percentile(samples, 2.0)" in js
    assert "percentile(samples, 98.0)" in js


def test_footprint_map_lazy_imagery_emits_button_and_libs(sample_item_dict):
    """End-to-end: rendering with lazy_imagery=True must include the
    CDN libs, the driver, and a per-item button keyed by the item's
    id."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = footprint_map([item], lazy_imagery=True).get_root().render()
    assert "umbra-sar-btn" in html
    assert "georaster-layer-for-leaflet" in html
    assert "umbraToggleSarImage" in html
    assert f'data-item-id="{item.id}"' in html


def test_footprint_map_lazy_imagery_off_by_default(sample_item_dict):
    """The default footprint_map call must NOT pull in the CDN libs
    or emit the button. Lazy imagery is opt-in."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = footprint_map([item]).get_root().render()
    assert "umbra-sar-btn" not in html
    assert "georaster" not in html


def test_timeline_map_lazy_imagery_emits_button_and_libs(sample_item_dict):
    """The timeline view must work identically -- click any footprint
    mid-animation and get the same fetch-on-demand SAR overlay."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = timeline_map([item], lazy_imagery=True).get_root().render()
    assert "umbra-sar-btn" in html
    assert "georaster-layer-for-leaflet" in html
    assert "umbraToggleSarImage" in html


def test_footprint_map_imagery_and_lazy_imagery_mutually_exclusive(sample_item_dict):
    """Both flags would try to add a SAR raster for each item; the
    library should reject the combo loudly rather than render a
    confused map."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    with pytest.raises(ValueError, match="lazy_imagery"):
        footprint_map([item], imagery=True, lazy_imagery=True)


def test_lazy_imagery_skips_items_with_no_resolvable_asset(monkeypatch, sample_item_dict):
    """Items whose GEC asset href can't be resolved must drop the
    button (instead of generating one with an empty URL that would
    just 404 in the browser). The popup itself still renders."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    # Force every asset_href call to return "" so resolution fails.
    monkeypatch.setattr(UmbraItem, "asset_href", lambda self, name: "")

    html = footprint_map([item], lazy_imagery=True).get_root().render()
    # The popup still renders, just without a button.
    assert item.id in html
    assert "umbra-sar-btn" not in html
    # And the driver isn't installed when no item has a URL --
    # otherwise we'd ship a 400 KB CDN bundle for nothing.
    assert "georaster-layer-for-leaflet" not in html


def test_cli_map_rejects_imagery_with_lazy_imagery(monkeypatch, tmp_path, sample_item_dict):
    """The CLI mirrors the library mutex: --imagery and --lazy-imagery
    are mutually exclusive."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    out = tmp_path / "x.html"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["map", "--imagery", "--lazy-imagery", "--out", str(out)],
    )
    assert result.exit_code != 0
    msg = result.output.lower()
    assert "imagery" in msg and "lazy" in msg


def test_cli_map_timeline_lazy_imagery_writes_button(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra map --timeline --lazy-imagery` produces an
    animated map whose popups each carry the fetch button + driver."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    out = tmp_path / "tl.html"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["map", "--timeline", "--lazy-imagery", "--no-geocode", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "umbra-sar-btn" in text
    assert "umbraToggleSarImage" in text
    # And the timeline plugin is still there -- this is the *combined*
    # view, not just one or the other.
    assert "timedimension" in text.lower() or "TimeDimension" in text


def test_cli_map_lazy_imagery_only_html(monkeypatch, tmp_path, sample_item_dict):
    """`--lazy-imagery` against a .geojson output makes no sense
    (GeoJSON has no rendering surface to attach a button to). The CLI
    must reject it cleanly."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    out = tmp_path / "x.geojson"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["map", "--lazy-imagery", "--out", str(out)],
    )
    assert result.exit_code != 0
    assert "lazy" in result.output.lower() and "html" in result.output.lower()
