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
    bytes before executing them. Without both, a
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
        "umbra_py.cli._shared.UmbraCatalog.search",
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
        "umbra_py.cli._shared.UmbraCatalog.search",
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
        "umbra_py.cli._shared.UmbraCatalog.search",
        lambda self, **_kwargs: iter([item]),
    )

    out = tmp_path / "x.geojson"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["map", "--lazy-imagery", "--out", str(out)],
    )
    assert result.exit_code != 0
    assert "lazy" in result.output.lower() and "html" in result.output.lower()


# --- the two map engines the one driver places overlays with ----------------


def test_driver_script_defaults_to_leaflet_placement():
    """Folium maps and the embedded-slice explorer are Leaflet pages; the
    default build must stay exactly the imageOverlay it always was."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "L.imageOverlay(dataUrl, bounds" in js
    assert "map.removeLayer(handle)" in js
    assert "addSource" not in js


def test_driver_script_maplibre_build_uses_an_image_source():
    """MapLibre GL has no imageOverlay: the equivalent is an `image` source
    (the data URL plus its four corners) drawn by a `raster` layer."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0, engine="maplibre")
    assert "map.addSource" in js and "type: 'image'" in js
    assert "type: 'raster'" in js
    assert "coordinates: [[west, north], [east, north], [east, south], [west, south]]" in js
    # Removal has to drop both halves, or a re-click leaves an orphan source.
    assert "map.removeLayer(ids.layer)" in js and "map.removeSource(ids.source)" in js
    # The Leaflet placement must not ride along on a page with no Leaflet.
    assert "L.imageOverlay" not in js


def test_maplibre_overlay_ids_are_sanitized():
    """Acquisition ids come from remote metadata and become MapLibre style
    keys, so anything outside [A-Za-z0-9_-] is collapsed first."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0, engine="maplibre")
    assert "replace(/[^A-Za-z0-9_-]/g, '_')" in js


def test_maplibre_overlay_slots_under_the_pages_own_layers():
    """MapLibre stacks by insertion order, so without a beforeId the image
    would bury the markers that opened it."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0, engine="maplibre")
    assert "window.umbraOverlayBeforeId" in js
    assert "(beforeId && map.getLayer(beforeId)) ? beforeId : undefined" in js


def test_both_engines_share_everything_above_the_placement():
    """The point of parametrising placement rather than forking the driver:
    the fetch, decode, stretch and button state machine stay one copy."""
    from umbra_py import _lazy_imagery as li

    shared = (
        "GeoTIFF.fromUrl",
        "pickOverview",
        "computeStretch",
        "rasterGeoreference",
        "rasterToDataURL",
        "window.umbraToggleSarImage",
        "layers[id] = addOverlay(map, id, dataUrl, geo ? geo.bounds : bounds);",
    )
    for engine in ("leaflet", "maplibre"):
        js = li.driver_script(percentile_low=2.0, percentile_high=98.0, engine=engine)
        for snippet in shared:
            assert snippet in js, (engine, snippet)


def test_unknown_engine_is_rejected():
    """A typo must fail loudly at build time, not emit a page whose button
    silently does nothing."""
    from umbra_py import _lazy_imagery as li

    with pytest.raises(ValueError, match="Unknown map engine"):
        li.driver_script(percentile_low=2.0, percentile_high=98.0, engine="openlayers")


# --- placing the overlay where the raster actually is -----------------------
#
# Regression cluster for the misaligned explorer overlay: a GEC is geocoded but
# not north-up (its grid is rotated to the collect geometry), so stretching the
# decoded overview onto the STAC footprint bbox rotated every scene off the map.


def test_driver_reads_georeferencing_from_the_file():
    """The driver must place the overlay from the raster's own affine and CRS,
    not from the item's footprint bbox. A GEC carries the rotated affine as a
    GeoTIFF ``ModelTransformation`` and its CRS as a geokey."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "ModelTransformation" in js
    assert "getGeoKeys" in js
    assert "ProjectedCSTypeGeoKey" in js and "GeographicTypeGeoKey" in js
    # Geo tags live on the full-res IFD, not on the overview that gets decoded,
    # so the picker has to hand back both.
    assert "{ base: base, image: chosen || fallback }" in js
    assert "rasterGeoreference(picked.base, picked.image, MAX_OUT_DIM)" in js


def test_driver_keeps_the_footprint_bbox_as_the_fallback():
    """A COG whose georeferencing we can't read must still render -- on the STAC
    bbox, exactly as the driver behaved before it read geo tags."""
    from umbra_py import _lazy_imagery as li

    js = li.driver_script(percentile_low=2.0, percentile_high=98.0)
    assert "layers[id] = addOverlay(map, id, dataUrl, geo ? geo.bounds : bounds);" in js
    # parseBounds() (the data-bounds attribute) is still required up front.
    assert "parseBounds" in js


def _run_georef_js(snippet: str):
    """Evaluate ``snippet`` against the driver's georeferencing chunk in node.

    The chunk is plain arithmetic over a couple of geotiff.js accessors, so it
    is the one part of the browser driver that can be checked for real rather
    than grepped for. ``snippet`` must ``console.log`` one JSON value.
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    from umbra_py import _lazy_imagery as li

    proc = subprocess.run(
        [node, "-e", li._GEOREF_OPS + snippet],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# A geotiff.js-shaped stub: just the four accessors the chunk calls.
_FAKE_IMAGE_JS = """
function fakeImage(w, h, fd, keys) {
  return {
    getWidth: function () { return w; },
    getHeight: function () { return h; },
    getFileDirectory: function () { return fd || {}; },
    getGeoKeys: function () { return keys || {}; }
  };
}
"""

# The real georeferencing of a 16001x16001 EPSG:4326 GEC over Black River,
# Jamaica (2025-10-29, Umbra-10) -- the acquisition the misalignment was
# reported on. Its grid is rotated ~77 degrees off north.
_ROTATED_GEC_JS = """
var MT = [6.697947955201462e-07, 2.8742001330176995e-06, 0, -77.87635378074701,
          2.7497634679268057e-06, -6.407965779008973e-07, 0, 18.010125116912462,
          0, 0, 0, 0, 0, 0, 0, 1];
var base = fakeImage(16001, 16001, { ModelTransformation: MT },
                     { GeographicTypeGeoKey: 4326 });
"""


def test_georeference_envelope_matches_the_rasters_own_bounds():
    """The overlay's placement box must be the envelope of the *rotated* grid's
    four corners -- which is what GDAL reports as the dataset bounds."""
    out = _run_georef_js(
        _FAKE_IMAGE_JS
        + _ROTATED_GEC_JS
        + """
var geo = rasterGeoreference(base, fakeImage(2001, 2001), 2048);
console.log(JSON.stringify(geo.bounds));
"""
    )
    (south, west), (north, east) = out
    assert west == pytest.approx(-77.87635378074701, abs=1e-9)
    assert south == pytest.approx(17.99987173086947, abs=1e-9)
    assert east == pytest.approx(-77.81964631789548, abs=1e-9)
    assert north == pytest.approx(18.054124082162758, abs=1e-9)


def test_georeference_pulls_each_output_pixel_from_the_rotated_grid():
    """The heart of the fix: an output pixel at a given lon/lat must resolve to
    the source pixel the raster's affine puts there. Before, output pixel
    (x, y) was source pixel (x, y) -- which is only true for a north-up grid."""
    out = _run_georef_js(
        _FAKE_IMAGE_JS
        + _ROTATED_GEC_JS
        + """
var w = 2001, sx = 16001 / w;
var geo = rasterGeoreference(base, fakeImage(w, w), 2048);
var west = geo.bounds[0][1], north = geo.bounds[1][0];
var lonPer = (geo.bounds[1][1] - west) / geo.width;
var latPer = (north - geo.bounds[0][0]) / geo.height;
var worst = 0, misses = 0;
// Walk the interior of the source grid (the outermost ring is a half-pixel
// sliver outside the rotated quad, and legitimately samples as transparent).
for (var u = 20; u < w - 20; u += 53) {
  for (var v = 20; v < w - 20; v += 53) {
    var lon = MT[3] + MT[0] * (u + 0.5) * sx + MT[1] * (v + 0.5) * sx;
    var lat = MT[7] + MT[4] * (u + 0.5) * sx + MT[5] * (v + 0.5) * sx;
    var i = geo.sourceIndex(Math.floor((lon - west) / lonPer),
                            Math.floor((north - lat) / latPer));
    if (i < 0) { misses++; continue; }
    var col = i % w, row = (i - col) / w;
    worst = Math.max(worst, Math.abs(col - u), Math.abs(row - v));
  }
}
console.log(JSON.stringify({ worst: worst, misses: misses }));
"""
    )
    assert out["misses"] == 0
    # Nearest neighbour through two floors, so one pixel of slack is the floor.
    assert out["worst"] <= 1


def test_georeference_inverts_utm_to_wgs84():
    """GECs also ship in WGS84 UTM zones, so the driver carries a UTM inverse.
    Checked against pyproj's answer for a real 32614 (North Dakota) GEC corner
    and a southern-hemisphere point (where the false northing applies)."""
    out = _run_georef_js(
        _FAKE_IMAGE_JS
        + """
var north = fakeImage(1, 1, {}, { ProjectedCSTypeGeoKey: 32614 });
var south = fakeImage(1, 1, {}, { ProjectedCSTypeGeoKey: 32733 });
console.log(JSON.stringify([
  modelToLonLat(north)(638879.6412305724, 5208544.292040982),
  modelToLonLat(south)(699000.0, 7100000.0)
]));
"""
    )
    (lon_n, lat_n), (lon_s, lat_s) = out
    # pyproj: Transformer.from_crs(32614, 4326, always_xy=True)
    assert lon_n == pytest.approx(-97.17268968652004, abs=1e-7)
    assert lat_n == pytest.approx(47.01583628252898, abs=1e-7)
    # pyproj: Transformer.from_crs(32733, 4326, always_xy=True)
    assert lon_s == pytest.approx(16.9916922806488, abs=1e-7)
    assert lat_s == pytest.approx(-26.205772891560926, abs=1e-7)


def test_georeference_leaves_a_north_up_raster_pixel_for_pixel():
    """A plain north-up COG (tiepoint + pixel scale) must come out unrotated and
    unresampled -- the fix must not degrade the case that already worked."""
    out = _run_georef_js(
        _FAKE_IMAGE_JS
        + """
var base = fakeImage(100, 50,
  { ModelTiepoint: [0, 0, 0, -10.0, 5.0, 0], ModelPixelScale: [0.01, 0.02, 0] },
  { GeographicTypeGeoKey: 4326 });
var geo = rasterGeoreference(base, fakeImage(100, 50), 2048);
console.log(JSON.stringify({
  bounds: geo.bounds, width: geo.width, height: geo.height,
  corner: geo.sourceIndex(0, 0), last: geo.sourceIndex(99, 49)
}));
"""
    )
    assert out["width"] == 100 and out["height"] == 50
    (south, west), (north, east) = out["bounds"]
    assert (west, south, east, north) == pytest.approx((-10.0, 4.0, -9.0, 5.0))
    assert out["corner"] == 0
    assert out["last"] == 49 * 100 + 99


@pytest.mark.parametrize(
    "image_js",
    [
        # No georeferencing tags at all.
        "fakeImage(10, 10, {}, { GeographicTypeGeoKey: 4326 })",
        # A CRS we can't invert without a projection library.
        "fakeImage(10, 10, { ModelTransformation: [1,0,0,0, 0,1,0,0, 0,0,0,0, 0,0,0,1] },"
        " { ProjectedCSTypeGeoKey: 3857 })",
    ],
)
def test_georeference_returns_null_when_it_cannot_place_the_raster(image_js):
    """Unreadable georeferencing must return null so the caller falls back to
    the STAC bbox, rather than throwing and losing the overlay entirely."""
    out = _run_georef_js(
        _FAKE_IMAGE_JS
        + f"""
var img = {image_js};
console.log(JSON.stringify(rasterGeoreference(img, img, 2048)));
"""
    )
    assert out is None
