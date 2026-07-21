"""Tests for the browser-side lazy SAR imagery overlay.

These exercise the Python side of the contract -- the rendered HTML
contains the right markers, the right URL, and the right driver. The
JS itself (geotiff.js) runs in a browser and isn't reachable from
pytest, so we deliberately stop at "the page asks for the right
things".
"""

from __future__ import annotations

import re

import pytest

from umbra_py.models import UmbraItem

# (min_lon, min_lat, max_lon, max_lat)
_BOUNDS = (-68.0, 10.4, -67.9, 10.5)


def test_popup_button_html_carries_id_url_and_bounds():
    """Each per-item button has to carry the item id (so the driver
    can dedupe layers), the asset URL (so the click handler can stream
    the COG without a server round-trip), and the footprint bounds (so
    the decoded overlay lands in the right place)."""
    from umbra_py._lazy_imagery import popup_button_html

    out = popup_button_html(
        item_id="abc-123",
        asset_url="https://example.com/scene.tif",
        bounds=_BOUNDS,
    )
    assert 'data-item-id="abc-123"' in out
    assert 'data-asset-url="https://example.com/scene.tif"' in out
    # data-bounds is "south,west,north,east".
    assert 'data-bounds="10.4,-68.0,10.5,-67.9"' in out
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
        bounds=_BOUNDS,
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


def test_cdn_url_pins_version():
    """A drifting CDN URL silently breaks browser-side decoding. The
    dep must be pinned so a release reproduces."""
    from umbra_py import _lazy_imagery as li

    assert re.search(r"geotiff@\d+\.\d+", li.GEOTIFF_JS), li.GEOTIFF_JS


def test_cdn_url_uses_published_browser_bundle_path():
    """Catch the obvious-but-painful failure mode: a CDN URL whose
    path doesn't correspond to a file the package actually publishes.
    geotiff's UMD browser bundle lives at ``dist-browser/geotiff.js``
    (the package's own ``unpkg`` field); a wrong path 404s and every
    click fails."""
    from umbra_py import _lazy_imagery as li

    assert li.GEOTIFF_JS.endswith("/dist-browser/geotiff.js"), li.GEOTIFF_JS


def test_sri_digest_is_a_pinned_sha384():
    """The integrity digest must be a real, pinned SHA-384 hash -- an
    empty or malformed value would either disable verification or block
    every load. We don't recompute the bytes here (the CDN host is
    egress-restricted in CI), just assert the shape."""
    from umbra_py import _lazy_imagery as li

    assert re.fullmatch(r"sha384-[A-Za-z0-9+/]+=*", li.GEOTIFF_SRI), li.GEOTIFF_SRI


def test_driver_script_verifies_geotiff_with_sri_and_cors():
    """The dynamically-injected geotiff.js ``<script>`` must carry the
    pinned Subresource Integrity digest and load with
    ``crossorigin='anonymous'`` so the browser verifies the fetched
    bytes before executing them (CODEBASE_ANALYSIS 3.4). Without both, a
    compromised CDN could run arbitrary script in every generated map."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    # The digest is carried as a JSON-encoded literal, same as the URL.
    assert '"' + li.GEOTIFF_SRI + '"' in js
    # And applied to the injected <script> element with a CORS fetch
    # (SRI is ignored by browsers on a no-cors request).
    assert "s.integrity = GEOTIFF_SRI" in js
    assert "s.crossOrigin = 'anonymous'" in js


def test_driver_script_finds_map_via_dom_and_loads_geotiff():
    """The driver must:

    1. Resolve the Folium map by DOM-walking from the clicked button
       to the enclosing ``.folium-map`` div, NOT by closing over a
       single ``map_var`` string. The closure approach went stale on
       Jupyter cell reruns and silently misrouted clicks in multi-map
       pages.
    2. Carry the geotiff.js CDN URL as a JSON-encoded JS string literal
       and ``appendChild`` it on first click (no workers, works from
       file://), instead of relying on pre-existing ``<script>`` tags.
    """
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "umbraToggleSarImage" in js
    # DOM-traversal lookup, not a stale `window['<baked-in-var>']`.
    assert "findMapForButton" in js
    assert "folium-map" in js
    # Both percentile cuts must reach the picker call sites.
    assert "pickPercentile(samples, 2.0)" in js
    assert "pickPercentile(samples, 98.0)" in js
    # The driver carries the pinned CDN URL as a JSON-encoded JS string
    # so a URL with quotes or non-ASCII can't break the template.
    assert '"' + li.GEOTIFF_JS + '"' in js
    # And injects it on demand.
    assert "document.head.appendChild" in js


def test_driver_script_decodes_with_main_thread_geotiff():
    """The driver must use bare geotiff.js on the main thread:
    ``GeoTIFF.fromUrl`` + ``readRasters`` + a canvas ``L.imageOverlay``.
    The previous georaster-layer-for-leaflet path spawned Web Workers,
    which Chromium refuses to start from ``file://`` -- the exact
    failure users hit. No worker, no georaster references."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "GeoTIFF.fromUrl" in js
    assert "readRasters" in js
    assert "L.imageOverlay" in js
    # The worker-spawning library must be gone entirely.
    assert "georaster" not in js.lower()
    assert "GeoRasterLayer" not in js


def test_driver_script_picks_a_cog_overview():
    """The driver must read a low-res overview, not the full-res image,
    so the fetch stays a few range requests."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "pickOverview" in js
    assert "getImageCount" in js


def test_driver_script_handles_degenerate_stretch_without_blacking_out():
    """Regression: the previous `hi = lo + 1` fallback was an
    *absolute* +1, which renders any low-amplitude raster (normalized
    SAR with values in [0, 0.05]) as solid black. Use a relative
    epsilon centered on the value so the image renders mid-gray."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "hi = lo + 1" not in js  # the broken old fallback
    # The new fallback derives delta from |lo| with a small relative
    # factor; spot-check that the factor is present.
    assert "Math.abs(lo)" in js
    assert "1e-3" in js


def test_driver_script_coerces_string_nodata_value():
    """Some COGs emit GDAL_NODATA as a string ("0"); strict ``===``
    against a numeric pixel would leak nodata into samples. The driver
    must Number()-coerce before comparing."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "normalizeNoData" in js
    assert "Number(raw)" in js


def test_driver_script_sorts_samples_once():
    """The percentile picks share a single in-place sort instead of
    `slice().sort()` per call."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "samples.slice().sort" not in js
    assert "samples.sort(" in js


def test_no_dead_helpers_exported():
    """`_verbatim_url_set` was added speculatively and never called;
    keep the module surface minimal."""
    from umbra_py import _lazy_imagery as li

    assert not hasattr(li, "_verbatim_url_set")


def test_footprint_map_lazy_imagery_emits_button_and_driver(sample_item_dict):
    """End-to-end: rendering with lazy_imagery=True must include the
    driver and a per-item button keyed by the item's id, AND must NOT
    inject geotiff.js as a bare ``<script src=...>`` tag into the head
    (it's loaded on demand from the driver instead)."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = footprint_map([item], lazy_imagery=True).get_root().render()
    assert "umbra-sar-btn" in html
    assert "umbraToggleSarImage" in html
    assert f'data-item-id="{item.id}"' in html
    # No bare <script src="...geotiff..."> tag in the head -- the driver
    # appendChild()s it on first click.
    assert not re.search(r'<script[^>]*src="[^"]*geotiff[^"]*"', html), html[:500]


def test_lazy_imagery_driver_loads_lib_on_click_not_in_head(sample_item_dict):
    """The CDN URL must live inside the driver IIFE (loaded on click),
    not as a bare ``<script src>`` in the head."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = footprint_map([item], lazy_imagery=True).get_root().render()

    # The URL appears inside the driver IIFE, not as a script src.
    assert "unpkg.com/geotiff" in html
    assert 'src="https://unpkg.com/geotiff' not in html
    # And the driver carries the dynamic-injection logic that
    # appendChild()s the <script> tag from JS on first click.
    assert "document.head.appendChild" in html


def test_footprint_map_lazy_imagery_off_by_default(sample_item_dict):
    """The default footprint_map call must NOT pull in the driver
    or emit the button. Lazy imagery is opt-in."""
    pytest.importorskip("folium")
    from umbra_py import footprint_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = footprint_map([item]).get_root().render()
    assert "umbra-sar-btn" not in html
    assert "umbraToggleSarImage" not in html
    assert "georaster" not in html


def test_timeline_map_lazy_imagery_emits_button_and_driver(sample_item_dict):
    """The timeline view must work identically -- click any footprint
    mid-animation and get the same fetch-on-demand SAR overlay."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = timeline_map([item], lazy_imagery=True).get_root().render()
    assert "umbra-sar-btn" in html
    assert "umbraToggleSarImage" in html
    # Same ordering guarantee as for footprint_map.
    assert not re.search(r'<script[^>]*src="[^"]*georaster[^"]*"', html)


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
    # otherwise we'd ship a CDN-loading shim for nothing.
    assert "umbraToggleSarImage" not in html
    assert "georaster" not in html


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
