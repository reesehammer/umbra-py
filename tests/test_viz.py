import json

import pytest

from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem
from umbra_py.viz import (
    _strip_z,
    item_to_feature,
    items_to_featurecollection,
    write_geojson,
)


def test_strip_z_handles_2d_and_3d():
    # A single 3D position becomes a 2D position.
    assert _strip_z([1.0, 2.0, 3.0]) == [1.0, 2.0]
    # A polygon ring is recursed through.
    ring = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
    assert _strip_z([ring]) == [[[1.0, 2.0], [4.0, 5.0]]]


def test_item_to_feature_strips_third_coordinate(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="http://example/item.json")
    feature = item_to_feature(item)

    assert feature["type"] == "Feature"
    assert feature["id"] == item.id
    assert feature["properties"]["stac_href"] == "http://example/item.json"
    assert feature["properties"]["product_type"] == "GEC"

    # Walk the coordinate tree: every leaf position should be exactly 2D.
    def assert_2d(node):
        if isinstance(node, list) and node and all(isinstance(v, (int, float)) for v in node):
            assert len(node) == 2
        elif isinstance(node, list):
            for child in node:
                assert_2d(child)

    assert_2d(feature["geometry"]["coordinates"])


def test_item_to_feature_falls_back_to_bbox():
    # An item with no geometry but a bbox should still produce a polygon.
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    feature = item_to_feature(item)
    assert feature["geometry"]["type"] == "Polygon"
    ring = feature["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1]  # closed
    assert len(ring) == 5


def test_item_to_feature_no_geometry_or_bbox():
    item = UmbraItem(id="x")
    feature = item_to_feature(item)
    assert feature["geometry"] is None


def test_featurecollection_unions_bbox(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    other = UmbraItem(id="other", bbox=(100.0, -10.0, 110.0, 0.0))
    fc = items_to_featurecollection([item, other])
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 2
    # The collection bbox spans both inputs.
    assert fc["bbox"][0] <= -68.0 and fc["bbox"][2] >= 110.0


def test_to_geojson_method(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    feature = item.to_geojson()
    assert feature["type"] == "Feature"
    assert feature["id"] == item.id


def test_write_geojson_roundtrip(tmp_path, sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    out = write_geojson([item], tmp_path / "out.geojson")
    data = json.loads(out.read_text())
    assert data["type"] == "FeatureCollection"
    assert data["features"][0]["id"] == item.id


def test_stretch_to_rgba_percentile_stretch():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _stretch_to_rgba

    # A linear ramp 0..99: the 2nd percentile ~= 2, 98th ~= 97.
    data = np.arange(100, dtype="float32").reshape(10, 10)
    rgba = _stretch_to_rgba(data, percentile=(2, 98))

    assert rgba.shape == (10, 10, 4)
    assert rgba.dtype.name == "uint8"
    # The very low pixel was clipped to zero (or near it); the high pixel maxed out.
    assert rgba[0, 0, 0] == 0
    assert rgba[-1, -1, 0] == 255
    # All visible pixels have full alpha; the zero pixel was treated as invalid.
    assert rgba[0, 0, 3] == 0  # value 0 is non-positive -> transparent
    assert rgba[5, 5, 3] == 255


def test_stretch_to_rgba_marks_invalid_pixels_transparent():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _stretch_to_rgba

    data = np.array([[1.0, 2.0, 3.0], [np.nan, 4.0, 5.0]], dtype="float32")
    rgba = _stretch_to_rgba(data)
    assert rgba[1, 0, 3] == 0  # NaN -> transparent
    assert rgba[0, 0, 3] == 255  # finite positive -> opaque


def test_stretch_to_rgba_all_invalid_raises():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _stretch_to_rgba

    data = np.zeros((4, 4), dtype="float32")  # all non-positive -> all invalid
    with pytest.raises(ValueError):
        _stretch_to_rgba(data)


def test_stretch_to_rgba_db_scaling_is_monotonic():
    """dB scaling compresses dynamic range but preserves ordering: the
    brightest valid pixel still maps to the highest grayscale value."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _stretch_to_rgba

    # Geometric ramp spans several orders of magnitude -- the case dB
    # scaling exists for. Linear and dB should agree on the extremes.
    data = np.array([[1.0, 10.0], [100.0, 1000.0]], dtype="float32")
    rgba = _stretch_to_rgba(data, percentile=(0, 100), db=True)

    assert rgba.shape == (2, 2, 4)
    assert rgba.dtype.name == "uint8"
    assert rgba[0, 0, 0] == 0  # smallest amplitude -> darkest
    assert rgba[1, 1, 0] == 255  # largest amplitude -> brightest
    # All four pixels are positive/finite, so all opaque.
    assert (rgba[..., 3] == 255).all()


def test_stretch_to_rgba_colormap_produces_color():
    """A colormap turns the grayscale stretch into RGB where the channels
    differ -- i.e. it's actually colored, not three equal gray channels."""
    pytest.importorskip("matplotlib")
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _stretch_to_rgba

    data = np.arange(1, 101, dtype="float32").reshape(10, 10)
    rgba = _stretch_to_rgba(data, colormap="viridis")

    assert rgba.shape == (10, 10, 4)
    # viridis is not a grayscale ramp: somewhere the R/G/B channels diverge.
    rgb = rgba[..., :3]
    assert not (rgb[..., 0] == rgb[..., 1]).all()


def test_quicklook_returns_pil_image(monkeypatch):
    """quicklook reads a (mocked) band and returns a correctly-sized
    RGBA PIL image without touching the network."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    data = np.linspace(1.0, 100.0, 12, dtype="float32").reshape(3, 4)
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))

    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    img = viz_mod.quicklook(item)
    assert img.size == (4, 3)  # PIL is (width, height)
    assert img.mode == "RGBA"


def test_save_quicklook_writes_png(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    data = np.arange(1, 17, dtype="float32").reshape(4, 4)
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))

    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    out = viz_mod.save_quicklook(item, tmp_path / "scene.png")
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_save_quicklook_jpeg_flattens_alpha(monkeypatch, tmp_path):
    """JPEG can't carry the transparency channel; the save must flatten to
    RGB rather than raising."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    # Include an invalid (<=0) pixel so the RGBA image actually has alpha.
    data = np.array([[0.0, 2.0], [3.0, 4.0]], dtype="float32")
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))

    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    out = viz_mod.save_quicklook(item, tmp_path / "scene.jpg")
    assert out.exists()


def test_cli_quicklook_writes_image(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra quicklook <url>` fetches the item JSON, renders a
    (mocked) band, and writes a PNG."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    data = np.arange(1, 65, dtype="float32").reshape(8, 8)
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))

    out = tmp_path / "scene.png"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["quicklook", "http://example/item.json", "--out", str(out), "--db"],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote quicklook" in result.output


def test_cli_quicklook_rejects_bad_percentile(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "quicklook",
            "http://example/item.json",
            "--out",
            str(tmp_path / "x.png"),
            "--percentile",
            "2",
        ],
    )
    assert result.exit_code != 0
    assert "percentile" in result.output.lower()


# -- gallery (HTML contact sheet) ------------------------------------------


def test_gallery_embeds_streamed_thumbnails(monkeypatch, sample_item_dict):
    """gallery() streams a thumbnail per item and assembles a standalone page.

    The thumbnail fetch and the viz-extra check are both mocked so the test
    stays offline.
    """
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(
        viz_mod,
        "_thumbnail_data_uri",
        lambda item, **_k: f"data:image/png;base64,{item.id}",
    )

    item = UmbraItem.from_dict(sample_item_dict)
    other = UmbraItem.from_dict({**sample_item_dict, "id": "second"})
    html = viz_mod.gallery([item, other], subtitle="rome")

    assert html.startswith("<!doctype html>")
    assert f'src="data:image/png;base64,{item.id}"' in html
    assert 'src="data:image/png;base64,second"' in html
    assert "rome" in html


def test_gallery_thumbnail_failure_falls_back_to_footprint(monkeypatch, sample_item_dict):
    """A thumbnail that can't be fetched must not sink the sheet -- the tile
    drops back to its footprint sketch."""
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", boom)

    item = UmbraItem.from_dict(sample_item_dict)
    html = viz_mod.gallery([item])  # must not raise
    assert "data:image" not in html
    assert "<svg" in html  # footprint fallback
    assert item.id in html


def test_save_gallery_writes_html(monkeypatch, tmp_path, sample_item_dict):
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", lambda *_a, **_k: "data:image/png;base64,Z")

    item = UmbraItem.from_dict(sample_item_dict)
    out = viz_mod.save_gallery([item], tmp_path / "g.html")
    assert out.exists()
    assert out.read_text().startswith("<!doctype html>")


def test_gallery_requires_viz_extra(monkeypatch, sample_item_dict):
    """Without rasterio, gallery() should fail fast with a clear message rather
    than quietly producing an all-footprint page."""
    from umbra_py import viz as viz_mod
    from umbra_py.exceptions import MissingDependencyError

    def no_rasterio(module):
        if module == "rasterio":
            raise MissingDependencyError("rasterio missing")

    monkeypatch.setattr(viz_mod, "_require", no_rasterio)
    with pytest.raises(MissingDependencyError):
        viz_mod.gallery([UmbraItem.from_dict(sample_item_dict)])


def test_cli_gallery_writes_html(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra gallery --area X` searches, streams (mocked)
    thumbnails, and writes a self-contained HTML page."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", lambda *_a, **_k: "data:image/png;base64,Z")

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([item]))

    out = tmp_path / "gallery.html"
    result = CliRunner().invoke(cli_mod.cli, ["gallery", "--area", "X", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "data:image/png;base64,Z" in out.read_text()
    assert "Wrote gallery" in result.output


def test_cli_gallery_rejects_non_html(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--area", "X", "--out", str(tmp_path / "x.png")]
    )
    assert result.exit_code != 0
    assert "html" in result.output.lower()


def test_cli_gallery_no_results(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter([]))
    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--area", "Nowhere", "--out", str(tmp_path / "g.html")]
    )
    assert result.exit_code != 0
    assert "No items matched" in result.output


def test_compose_change_rgba_identical_bands_are_gray():
    """Two identical passes have no change, so every valid pixel lands on
    the gray diagonal (R == G == B)."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    band = np.linspace(1.0, 100.0, 16, dtype="float32").reshape(4, 4)
    rgba = _compose_change_rgba([band, band])

    assert rgba.shape == (4, 4, 4)
    assert rgba.dtype.name == "uint8"
    rgb = rgba[..., :3]
    assert (rgb[..., 0] == rgb[..., 1]).all()
    assert (rgb[..., 1] == rgb[..., 2]).all()
    assert (rgba[..., 3] == 255).all()


def test_compose_change_rgba_two_dates_color_semantics():
    """Backscatter that appears in the later pass reads green; backscatter
    that vanishes reads magenta."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    # Pixel (0,0): bright then dark -> "lost". Pixel (0,1): dark then bright
    # -> "gained". A spread of mid values gives the stretch something to
    # work with at the percentile extremes.
    t1 = np.array([[100.0, 1.0, 40.0], [50.0, 60.0, 70.0]], dtype="float32")
    t2 = np.array([[1.0, 100.0, 40.0], [50.0, 60.0, 70.0]], dtype="float32")
    rgba = _compose_change_rgba([t1, t2], percentile=(0, 100))

    lost = rgba[0, 0]  # was bright, now dark
    gained = rgba[0, 1]  # was dark, now bright
    # Lost -> magenta: red and blue high, green low (R=t1, G=t2, B=t1).
    assert lost[0] > lost[1] and lost[2] > lost[1]
    # Gained -> green: green high, red and blue low.
    assert gained[1] > gained[0] and gained[1] > gained[2]


def test_compose_change_rgba_three_dates_map_to_rgb():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    # Each band needs internal contrast for the percentile stretch to bite.
    # Pixel (0,0) is dark in t1/t3 and bright only in t2 -> green.
    spread = [40.0, 50.0, 60.0, 70.0, 100.0]
    t1 = np.array([[1.0, *spread[:2]], [*spread[2:]]], dtype="float32")
    t2 = np.array([[100.0, *spread[:2]], [spread[2], spread[3], 1.0]], dtype="float32")
    t3 = t1.copy()
    rgba = _compose_change_rgba([t1, t2, t3], percentile=(0, 100))
    px = rgba[0, 0]
    assert px[1] > px[0] and px[1] > px[2]


def test_compose_change_rgba_invalid_pixels_propagate():
    """A pixel invalid in any one band is transparent in the composite."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    t1 = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    t2 = np.array([[np.nan, 2.0], [3.0, 4.0]], dtype="float32")
    rgba = _compose_change_rgba([t1, t2])
    assert rgba[0, 0, 3] == 0  # invalid in t2 -> transparent
    assert rgba[1, 1, 3] == 255  # valid in both -> opaque


def test_compose_change_rgba_rejects_bad_band_count():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    band = np.ones((2, 2), dtype="float32")
    with pytest.raises(ValueError, match="2 or 3"):
        _compose_change_rgba([band])
    with pytest.raises(ValueError, match="2 or 3"):
        _compose_change_rgba([band, band, band, band])


def test_compose_change_rgba_rejects_mismatched_shapes():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_change_rgba

    with pytest.raises(ValueError, match="same shape"):
        _compose_change_rgba([np.ones((2, 2)), np.ones((2, 3))])


def test_change_composite_returns_pil_image(monkeypatch):
    """change_composite stacks (mocked) co-registered bands into an RGBA
    PIL image without touching the network."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    t1 = np.linspace(1.0, 100.0, 12, dtype="float32").reshape(3, 4)
    t2 = t1[::-1].copy()
    monkeypatch.setattr(viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t2], (0, 0, 1, 1)))

    a = UmbraItem(id="a", bbox=(0.0, 0.0, 1.0, 1.0))
    b = UmbraItem(id="b", bbox=(0.0, 0.0, 1.0, 1.0))
    img = viz_mod.change_composite([a, b])
    assert img.size == (4, 3)  # PIL is (width, height)
    assert img.mode == "RGBA"


def test_change_composite_rejects_wrong_item_count(monkeypatch):
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    # Guard against the network: the count check must fire first.
    monkeypatch.setattr(
        viz_mod,
        "_coregister_bands",
        lambda *a, **k: pytest.fail("should not co-register a bad item count"),
    )
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    with pytest.raises(ValueError, match="2 or 3"):
        viz_mod.change_composite([item])


def test_save_change_composite_writes_png(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    t1 = np.arange(1, 17, dtype="float32").reshape(4, 4)
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t1[::-1].copy()], (0, 0, 1, 1))
    )

    a = UmbraItem(id="a", bbox=(0.0, 0.0, 1.0, 1.0))
    b = UmbraItem(id="b", bbox=(0.0, 0.0, 1.0, 1.0))
    out = viz_mod.save_change_composite([a, b], tmp_path / "change.png")
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_save_change_composite_jpeg_flattens_alpha(monkeypatch, tmp_path):
    """JPEG can't carry transparency; the save flattens rather than raising."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    t1 = np.array([[0.0, 2.0], [3.0, 4.0]], dtype="float32")  # has an invalid pixel
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t1.copy()], (0, 0, 1, 1))
    )

    a = UmbraItem(id="a", bbox=(0.0, 0.0, 1.0, 1.0))
    b = UmbraItem(id="b", bbox=(0.0, 0.0, 1.0, 1.0))
    out = viz_mod.save_change_composite([a, b], tmp_path / "change.jpg")
    assert out.exists()


def test_cli_change_writes_image(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra change <url> <url>` fetches each item, stacks the
    (mocked) co-registered bands, and writes a PNG."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    t1 = np.arange(1, 65, dtype="float32").reshape(8, 8)
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t1[::-1].copy()], (0, 0, 1, 1))
    )

    out = tmp_path / "change.png"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "change",
            "http://example/a.json",
            "http://example/b.json",
            "--out",
            str(out),
            "--db",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote change composite" in result.output


def test_cli_change_rejects_single_url(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        ["change", "http://example/a.json", "--out", str(tmp_path / "x.png")],
    )
    assert result.exit_code != 0
    assert "2 or 3" in result.output


def _dated_item(item_id, dt, pol="VV"):
    return UmbraItem(
        id=item_id,
        bbox=(0.0, 0.0, 1.0, 1.0),
        properties={"datetime": dt, "sar:polarizations": [pol]},
    )


def test_select_change_frames_spans_range_two():
    """frames=2 returns the earliest and latest acquisition."""
    from umbra_py.viz import select_change_frames

    items = [
        _dated_item("b", "2024-02-01T00:00:00Z"),
        _dated_item("d", "2024-04-01T00:00:00Z"),
        _dated_item("a", "2024-01-01T00:00:00Z"),
        _dated_item("c", "2024-03-01T00:00:00Z"),
    ]
    chosen = select_change_frames(items, frames=2)
    assert [i.id for i in chosen] == ["a", "d"]


def test_select_change_frames_three_picks_endpoints_and_middle():
    from umbra_py.viz import select_change_frames

    items = [_dated_item(str(k), f"2024-0{k}-01T00:00:00Z") for k in range(1, 6)]
    chosen = select_change_frames(items, frames=3)
    # Five evenly-spaced dates -> first, middle, last.
    assert [i.id for i in chosen] == ["1", "3", "5"]


def test_select_change_frames_prefers_largest_polarization_group():
    """Mixing HH and VV would show the polarization difference as fake
    change, so the largest single-pol group wins."""
    from umbra_py.viz import select_change_frames

    items = [
        _dated_item("vv1", "2024-01-01T00:00:00Z", pol="VV"),
        _dated_item("hh", "2024-02-01T00:00:00Z", pol="HH"),
        _dated_item("vv2", "2024-03-01T00:00:00Z", pol="VV"),
        _dated_item("vv3", "2024-04-01T00:00:00Z", pol="VV"),
    ]
    chosen = select_change_frames(items, frames=2)
    assert [i.id for i in chosen] == ["vv1", "vv3"]
    assert all(i.polarizations == ["VV"] for i in chosen)


def test_select_change_frames_falls_back_to_mixed_pol_when_no_pair():
    """If every acquisition is a different polarization, comparing across
    them is the best available -- return them rather than failing."""
    from umbra_py.viz import select_change_frames

    items = [
        _dated_item("hh", "2024-01-01T00:00:00Z", pol="HH"),
        _dated_item("vv", "2024-02-01T00:00:00Z", pol="VV"),
    ]
    chosen = select_change_frames(items, frames=2)
    assert {i.id for i in chosen} == {"hh", "vv"}


def test_select_change_frames_drops_undated_and_requires_two():
    from umbra_py.viz import select_change_frames

    undated = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0), properties={})
    one = _dated_item("a", "2024-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="at least 2"):
        select_change_frames([undated, one], frames=2)


def test_select_change_frames_rejects_bad_frames():
    from umbra_py.viz import select_change_frames

    items = [_dated_item("a", "2024-01-01T00:00:00Z"), _dated_item("b", "2024-02-01T00:00:00Z")]
    with pytest.raises(ValueError, match="2, 3, or None"):
        select_change_frames(items, frames=4)


def test_select_change_frames_none_returns_whole_series():
    """frames=None returns the full single-polarization series, oldest-first,
    for an animated time-lapse."""
    from umbra_py.viz import select_change_frames

    items = [_dated_item(str(k), f"2024-0{k}-01T00:00:00Z") for k in (4, 1, 3, 2)]
    chosen = select_change_frames(items, frames=None)
    assert [i.id for i in chosen] == ["1", "2", "3", "4"]


def test_change_animation_returns_ordered_frames(monkeypatch):
    """change_animation co-registers (mocked) and returns one RGB frame per
    acquisition, oldest-first."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    later = _dated_item("late", "2024-03-01T00:00:00Z")
    early = _dated_item("early", "2024-01-01T00:00:00Z")
    bands = [np.arange(1, 13, dtype="float32").reshape(3, 4) for _ in range(2)]
    captured = {}

    def fake_coreg(items, asset, max_size):
        captured["order"] = [i.id for i in items]
        return bands, (0, 0, 1, 1)

    monkeypatch.setattr(viz_mod, "_coregister_bands", fake_coreg)
    frames = viz_mod.change_animation([later, early])
    # Sorted oldest-first before co-registration so frames play forward.
    assert captured["order"] == ["early", "late"]
    assert len(frames) == 2
    assert all(f.mode == "RGB" and f.size == (4, 3) for f in frames)


def test_change_animation_requires_two(monkeypatch):
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: pytest.fail("should not co-register")
    )
    with pytest.raises(ValueError, match="at least 2"):
        viz_mod.change_animation([_dated_item("only", "2024-01-01T00:00:00Z")])


def test_save_change_animation_writes_animated_gif(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    PIL = pytest.importorskip("PIL")  # noqa: N806
    from umbra_py import viz as viz_mod

    # Distinct spatial patterns per frame (a rolled bright ramp) so the
    # stretched frames really differ -- identical frames get optimized away.
    base = np.arange(1, 17, dtype="float32").reshape(4, 4)
    bands = [np.roll(base, k, axis=0) for k in range(3)]
    items = [_dated_item(str(k), f"2024-0{k + 1}-01T00:00:00Z") for k in range(3)]
    monkeypatch.setattr(viz_mod, "_coregister_bands", lambda *a, **k: (bands, (0, 0, 1, 1)))

    out = viz_mod.save_change_animation(items, tmp_path / "lapse.gif", fps=4)
    assert out.exists()
    assert out.read_bytes()[:4] == b"GIF8"
    with PIL.Image.open(out) as im:
        assert getattr(im, "n_frames", 1) == 3  # one frame per acquisition


def test_cli_change_gif_animates_search_results(monkeypatch, tmp_path):
    """`umbra change --area X --out lapse.gif` animates every matched
    acquisition (not just 2-3)."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    found = [_dated_item(str(k), f"2024-{k:02d}-01T00:00:00Z") for k in range(1, 6)]
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter(found))
    bands = [np.arange(1, 65, dtype="float32").reshape(8, 8) + k for k in range(5)]
    monkeypatch.setattr(viz_mod, "_coregister_bands", lambda *a, **k: (bands, (0, 0, 1, 1)))

    out = tmp_path / "lapse.gif"
    result = CliRunner().invoke(cli_mod.cli, ["change", "--area", "X", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    # All five matched frames were used (the 2-3 cap doesn't apply to .gif).
    assert "Selected 5 of 5" in result.output
    assert "Wrote time-lapse" in result.output


def test_cli_change_gif_allows_many_explicit_urls(monkeypatch, tmp_path, sample_item_dict):
    """The 2-3 URL cap is lifted for .gif output."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _u: sample_item_dict)
    bands = [np.arange(1, 65, dtype="float32").reshape(8, 8) + k for k in range(4)]
    monkeypatch.setattr(viz_mod, "_coregister_bands", lambda *a, **k: (bands, (0, 0, 1, 1)))

    urls = [f"http://example/{k}.json" for k in range(4)]
    out = tmp_path / "lapse.gif"
    result = CliRunner().invoke(cli_mod.cli, ["change", *urls, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_cli_change_png_still_caps_at_three(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _u: sample_item_dict)
    urls = [f"http://example/{k}.json" for k in range(4)]
    result = CliRunner().invoke(cli_mod.cli, ["change", *urls, "--out", str(tmp_path / "c.png")])
    assert result.exit_code != 0
    assert "2 or 3" in result.output


def test_cli_change_colormap_requires_gif(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _u: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "change",
            "http://example/a.json",
            "http://example/b.json",
            "--out",
            str(tmp_path / "c.png"),
            "--colormap",
            "magma",
        ],
    )
    assert result.exit_code != 0
    assert "colormap" in result.output.lower()


def test_cli_change_search_mode_selects_and_renders(monkeypatch, tmp_path):
    """`umbra change --area X` searches, auto-selects frames, and renders."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    found = [
        _dated_item("old", "2024-01-01T00:00:00Z"),
        _dated_item("mid", "2024-02-01T00:00:00Z"),
        _dated_item("new", "2024-03-01T00:00:00Z"),
    ]
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", lambda self, **_kw: iter(found))
    t1 = np.arange(1, 65, dtype="float32").reshape(8, 8)
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t1[::-1].copy()], (0, 0, 1, 1))
    )

    out = tmp_path / "change.png"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["change", "--area", "Centerfield", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    # The earliest and latest were chosen (frames defaults to 2).
    assert "old" in result.output and "new" in result.output
    assert "Wrote change composite" in result.output


def test_cli_change_rejects_urls_and_search_together(monkeypatch, sample_item_dict, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _u: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        ["change", "http://example/a.json", "--area", "X", "--out", str(tmp_path / "o.png")],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_cli_change_search_too_few_results(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kw: iter([_dated_item("only", "2024-01-01T00:00:00Z")]),
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        ["change", "--area", "Nowhere", "--out", str(tmp_path / "o.png")],
    )
    assert result.exit_code != 0
    assert "at least 2" in result.output


def test_cli_change_no_urls_no_search_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(cli_mod.cli, ["change", "--out", str(tmp_path / "o.png")])
    assert result.exit_code != 0
    assert "item URLs" in result.output


def test_centroid_from_bbox():
    from umbra_py.viz import _centroid

    item = UmbraItem(id="x", bbox=(-2.0, 10.0, 4.0, 20.0))
    # (lat, lon) = ((10+20)/2, (-2+4)/2)
    assert _centroid(item) == (15.0, 1.0)


def test_centroid_returns_none_without_bbox():
    from umbra_py.viz import _centroid

    assert _centroid(UmbraItem(id="x")) is None


def test_footprint_map_includes_centroid_marker_and_legend(sample_item_dict):
    pytest.importorskip("folium")
    from umbra_py import viz as viz_mod

    item = UmbraItem.from_dict(sample_item_dict)
    html = viz_mod.footprint_map([item]).get_root().render()

    # Centroid marker is always drawn so the item is visible at any zoom.
    assert "circleMarker" in html  # folium emits L.circleMarker(...)
    # Legend is pinned to the corner with the count.
    assert "Umbra footprints" in html
    assert "1 footprint" in html


def test_footprint_map_legend_distinguishes_imagery_when_enabled(monkeypatch, sample_item_dict):
    pytest.importorskip("folium")
    from umbra_py import viz as viz_mod

    def fake_overlay(item, **_kwargs):
        if item.id == "bad":
            raise OSError("404")

        class _FakeLayer:
            def add_to(self, _m):
                return self

        return _FakeLayer()

    monkeypatch.setattr(viz_mod, "image_overlay", fake_overlay)

    good = UmbraItem.from_dict(sample_item_dict)
    bad = UmbraItem(id="bad", bbox=(10.0, 10.0, 11.0, 11.0))

    with pytest.warns(UserWarning):
        m = viz_mod.footprint_map([good, bad], imagery=True)
    html = m.get_root().render()

    assert "1 with SAR imagery" in html
    assert "1 footprint only" in html


def test_footprint_map_imagery_skips_unreachable_items(monkeypatch, sample_item_dict):
    """When imagery=True hits a 404 / network error for one item, the map
    should still render the rest -- not crash the whole call."""
    pytest.importorskip("folium")
    from umbra_py import viz as viz_mod

    seen: list[str] = []

    def fake_overlay(item, **_kwargs):
        seen.append(item.id)
        # First item simulates a 404 / unreachable data; second succeeds.
        if item.id == "bad":
            raise OSError("HTTP response code: 404")

        class _FakeLayer:
            def add_to(self, _m):
                return self

        return _FakeLayer()

    monkeypatch.setattr(viz_mod, "image_overlay", fake_overlay)

    good = UmbraItem.from_dict(sample_item_dict)
    bad = UmbraItem(id="bad", bbox=(10.0, 10.0, 11.0, 11.0))

    with pytest.warns(UserWarning, match="Skipping SAR overlay for 'bad'"):
        m = viz_mod.footprint_map([bad, good], imagery=True)

    assert {"bad", good.id} == set(seen), "both items should be attempted"
    assert m is not None  # the map still rendered


def test_image_overlay_raises_clear_error_on_empty_url(monkeypatch):
    """If asset_href returns '' (no task_id, no populated href), don't pass an
    empty string to rasterio -- raise something the caller can act on."""
    pytest.importorskip("folium")
    pytest.importorskip("rasterio")
    from umbra_py import viz as viz_mod
    from umbra_py.exceptions import AssetNotFoundError

    item = UmbraItem(
        id="x",
        bbox=(0.0, 0.0, 1.0, 1.0),
        assets={"foo.tif": {"href": ""}},
        properties={},  # no umbra:task_id -> asset_href returns ""
    )
    # Force asset_href to return "" without going through asset_map lookups.
    monkeypatch.setattr(UmbraItem, "asset_href", lambda self, name: "")
    with pytest.raises(AssetNotFoundError, match="no resolvable URL"):
        viz_mod.image_overlay(item)


def test_footprint_map_without_extra_raises(monkeypatch, sample_item_dict):
    # Simulate folium not being installed.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "folium":
            raise ImportError("no folium")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    item = UmbraItem.from_dict(sample_item_dict)

    from umbra_py.viz import footprint_map  # re-import under patched env

    with pytest.raises(MissingDependencyError):
        footprint_map([item])


def _reset_geocode_state():
    from umbra_py import viz as viz_mod

    viz_mod._GEOCODE_CACHE.clear()
    viz_mod._LAST_GEOCODE_AT = 0.0


def test_reverse_geocode_returns_display_name(monkeypatch):
    from umbra_py import viz as viz_mod

    _reset_geocode_state()
    calls: list[dict] = []

    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"display_name": "Reykjavík, Iceland"}

    class _FakeSession:
        def get(self, url, params=None, timeout=None, headers=None):
            calls.append({"url": url, "params": params})
            return _FakeResp()

    # Avoid the 1 s throttle entirely in tests.
    monkeypatch.setattr(__import__("time"), "sleep", lambda _s: None)

    label = viz_mod._reverse_geocode(64.13, -21.94, session=_FakeSession())
    assert label == "Reykjavík, Iceland"
    assert calls and calls[0]["params"]["format"] == "jsonv2"

    # Second call at the same (rounded) coordinate must hit the cache,
    # not the network.
    label2 = viz_mod._reverse_geocode(64.13, -21.94, session=_FakeSession())
    assert label2 == "Reykjavík, Iceland"
    assert len(calls) == 1, "second call should be cached, not re-requested"


def test_reverse_geocode_swallows_network_errors(monkeypatch):
    import requests

    from umbra_py import viz as viz_mod

    _reset_geocode_state()

    class _BrokenSession:
        def get(self, *_a, **_k):
            raise requests.ConnectionError("boom")

    monkeypatch.setattr(__import__("time"), "sleep", lambda _s: None)
    label = viz_mod._reverse_geocode(0.0, 0.0, session=_BrokenSession())
    assert label is None
    # And the miss is cached so we don't hammer the service.
    assert (0, 0, 10) in viz_mod._GEOCODE_CACHE


def test_footprint_map_geocode_adds_location_row(monkeypatch, sample_item_dict):
    pytest.importorskip("folium")
    from umbra_py import viz as viz_mod

    _reset_geocode_state()
    monkeypatch.setattr(
        viz_mod,
        "_reverse_geocode",
        lambda lat, lon, **_kw: f"Somewhere near {lat:.1f},{lon:.1f}",
    )
    monkeypatch.setattr(viz_mod, "_require_session_for_geocoding", lambda: None)

    item = UmbraItem.from_dict(sample_item_dict)
    m = viz_mod.footprint_map([item], geocode=True)
    html = m.get_root().render()
    assert "Location" in html
    assert "Somewhere near" in html


def test_footprint_map_default_does_not_geocode(monkeypatch, sample_item_dict):
    """The library default is opt-in; library callers don't pay for a
    surprise network call when they just want a footprint map."""
    pytest.importorskip("folium")
    from umbra_py import viz as viz_mod

    def _boom(*_a, **_k):
        raise AssertionError("_reverse_geocode must not be called by default")

    monkeypatch.setattr(viz_mod, "_reverse_geocode", _boom)
    item = UmbraItem.from_dict(sample_item_dict)
    viz_mod.footprint_map([item])  # must not raise


def test_timeline_map_emits_timestamped_geojson(sample_item_dict):
    """The timeline map embeds each item as a TimestampedGeoJson feature
    keyed by its acquisition datetime, with the metadata popup attached."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = timeline_map([item]).get_root().render()

    # The plugin's JS bundle is loaded.
    assert "leaflet.timedimension" in html.lower() or "TimeDimension" in html
    # The item's ISO timestamp appears in the feature payload.
    assert item.datetime.isoformat() in html
    # The popup metadata is carried through (item id renders into the popup).
    assert item.id in html


def test_timeline_map_skips_items_missing_datetime_or_geometry():
    """Items without a datetime or geometry can't be placed on the
    timeline; they're silently dropped so a single bad item doesn't
    blank the whole map."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map

    no_geom = UmbraItem(id="no-geom", properties={"datetime": "2024-02-01T00:00:00Z"})
    no_dt = UmbraItem(id="no-dt", bbox=(0.0, 0.0, 1.0, 1.0))
    # Both must be skipped without raising. The empty-feature map still
    # renders -- just without the slider control.
    m = timeline_map([no_geom, no_dt])
    assert m is not None


def test_timeline_map_passes_period_through(sample_item_dict):
    """Custom --timeline-period reaches the plugin so users can pick
    PT1H vs P7D vs P1D depending on their search density."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map

    item = UmbraItem.from_dict(sample_item_dict)
    html = timeline_map([item], period="PT1H").get_root().render()
    assert "PT1H" in html


def test_save_timeline_map_writes_html(tmp_path, sample_item_dict):
    pytest.importorskip("folium")
    from umbra_py import save_timeline_map

    item = UmbraItem.from_dict(sample_item_dict)
    out = save_timeline_map([item], tmp_path / "tl.html")
    assert out.exists()
    text = out.read_text()
    assert "<html" in text.lower()
    # Sanity: the timeline plugin was emitted, not a static footprint map.
    assert "timedimension" in text.lower() or "TimeDimension" in text


def test_cli_map_rejects_timeline_with_imagery(monkeypatch, tmp_path, sample_item_dict):
    """--timeline + --imagery isn't supported yet; the CLI should reject
    the combo with a clear error instead of producing a confused map."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    runner = CliRunner()
    out = tmp_path / "x.html"
    result = runner.invoke(
        cli_mod.cli,
        ["map", "--timeline", "--imagery", "--out", str(out)],
    )
    assert result.exit_code != 0
    assert "timeline" in result.output.lower() and "imagery" in result.output.lower()


def test_timeline_map_geocode_threads_label_into_popup(monkeypatch, sample_item_dict):
    """timeline_map(geocode=True) should resolve a place name per item
    and bake it into the popup HTML that TimestampedGeoJson carries.
    The plugin renders feature properties verbatim, so the label has
    to be in place at generation time."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map
    from umbra_py import viz as viz_mod

    _reset_geocode_state()
    monkeypatch.setattr(
        viz_mod,
        "_reverse_geocode",
        lambda lat, lon, **_kw: f"Somewhere near {lat:.1f},{lon:.1f}",
    )
    monkeypatch.setattr(viz_mod, "_require_session_for_geocoding", lambda: None)

    item = UmbraItem.from_dict(sample_item_dict)
    html = timeline_map([item], geocode=True).get_root().render()
    assert "Location" in html
    assert "Somewhere near" in html


def test_timeline_map_default_does_not_geocode(monkeypatch, sample_item_dict):
    """Library default stays opt-in: calling timeline_map() without
    geocode=True must not hit the network."""
    pytest.importorskip("folium")
    from umbra_py import timeline_map
    from umbra_py import viz as viz_mod

    def _boom(*_a, **_k):
        raise AssertionError("_reverse_geocode must not be called by default")

    monkeypatch.setattr(viz_mod, "_reverse_geocode", _boom)
    item = UmbraItem.from_dict(sample_item_dict)
    timeline_map([item])  # must not raise


def test_cli_map_timeline_with_geocode_flows_through(monkeypatch, tmp_path, sample_item_dict):
    """`umbra map --timeline --geocode` should reach save_timeline_map
    with geocode=True. We patch the geocoder so the test stays offline."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    _reset_geocode_state()
    # Stick to ASCII -- folium JSON-encodes popup properties with
    # ensure_ascii=True, so non-ASCII labels arrive in the rendered
    # HTML as \uXXXX escapes and would defeat a naive substring check.
    monkeypatch.setattr(viz_mod, "_reverse_geocode", lambda lat, lon, **_kw: "Test Town")
    monkeypatch.setattr(viz_mod, "_require_session_for_geocoding", lambda: None)

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    out = tmp_path / "tl.html"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["map", "--timeline", "--geocode", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "Test Town" in text


def test_cli_map_timeline_writes_animated_html(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end check: `umbra map --timeline` invokes the timeline
    renderer (not the static map) and produces a slider-bearing HTML
    file."""
    pytest.importorskip("folium")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    item = UmbraItem.from_dict(sample_item_dict)
    monkeypatch.setattr(
        cli_mod.UmbraCatalog,
        "search",
        lambda self, **_kwargs: iter([item]),
    )

    runner = CliRunner()
    out = tmp_path / "tl.html"
    result = runner.invoke(cli_mod.cli, ["map", "--timeline", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    text = out.read_text()
    assert "timedimension" in text.lower() or "TimeDimension" in text


def _swipe_items():
    a = UmbraItem(
        id="before",
        bbox=(0.0, 0.0, 1.0, 1.0),
        properties={"datetime": "2024-01-01T00:00:00Z", "sar:polarizations": ["VV"]},
    )
    b = UmbraItem(
        id="after",
        bbox=(0.0, 0.0, 1.0, 1.0),
        properties={"datetime": "2024-06-01T00:00:00Z", "sar:polarizations": ["VV"]},
    )
    return a, b


def _fake_coregister(np):
    """A mock for _coregister_bands: two pixel-aligned bands sharing bounds."""
    t1 = np.linspace(1.0, 100.0, 16, dtype="float32").reshape(4, 4)
    t2 = t1[::-1].copy()
    return lambda *a, **k: ([t1, t2], (0.0, 0.0, 1.0, 1.0))


def test_swipe_map_has_sidebyside_and_two_overlays(monkeypatch):
    """swipe_map builds two image overlays and a side-by-side control,
    plus the ImageOverlay.getContainer shim the plugin needs to clip them."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_coregister_bands", _fake_coregister(np))

    before, after = _swipe_items()
    m = viz_mod.swipe_map(before, after)
    html_text = m.get_root().render()

    assert "L.control.sideBySide" in html_text
    # The shim points getContainer at the overlay's pane so the plugin clips
    # in the correct (layer-point) coordinate space.
    assert "getContainer = function() { return this.getPane(); }" in html_text
    # Each overlay lives in its own pane so the two clips stay independent.
    assert '"sbsBefore"' in html_text and '"sbsAfter"' in html_text
    # Two SAR overlays (the base64 PNGs) were embedded.
    assert html_text.count("data:image/png;base64,") == 2


def test_swipe_map_db_reaches_stretch(monkeypatch):
    """db=True must reach the dB stretch for both co-registered bands."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_coregister_bands", _fake_coregister(np))
    seen_db = []
    real_stretch = viz_mod._stretch_to_rgba

    def spy(data, **kwargs):
        seen_db.append(kwargs.get("db"))
        return real_stretch(data, **kwargs)

    monkeypatch.setattr(viz_mod, "_stretch_to_rgba", spy)

    before, after = _swipe_items()
    viz_mod.swipe_map(before, after, db=True)
    assert seen_db == [True, True]  # one per overlay


def test_swipe_map_overlays_share_one_grid(monkeypatch):
    """Both overlays must be placed on the *same* (co-registered) bounds so
    the swipe compares identical ground -- the fix for the misaligned seam."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_coregister_bands", _fake_coregister(np))
    before, after = _swipe_items()
    m = viz_mod.swipe_map(before, after)
    overlays = [c for c in m._children.values() if c.__class__.__name__ == "ImageOverlay"]
    assert len(overlays) == 2
    assert overlays[0].bounds == overlays[1].bounds == [[0.0, 0.0], [1.0, 1.0]]


def test_save_swipe_map_writes_html(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(viz_mod, "_coregister_bands", _fake_coregister(np))
    before, after = _swipe_items()
    out = viz_mod.save_swipe_map(before, after, tmp_path / "swipe.html")
    assert out.exists()
    assert "sideBySide" in out.read_text()


def test_cli_swipe_writes_html(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra swipe <url> <url>` fetches both items and writes HTML."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(viz_mod, "_coregister_bands", _fake_coregister(np))

    out = tmp_path / "swipe.html"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["swipe", "http://example/a.json", "http://example/b.json", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote swipe map" in result.output


def test_cli_swipe_rejects_single_url(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        ["swipe", "http://example/a.json", "--out", str(tmp_path / "x.html")],
    )
    assert result.exit_code != 0
    assert "exactly 2" in result.output


def test_cli_swipe_rejects_urls_and_search_mode(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "swipe",
            "http://example/a.json",
            "http://example/b.json",
            "--area",
            "Centerfield",
            "--out",
            str(tmp_path / "x.html"),
        ],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


# --- timescan composite ------------------------------------------------------


def test_compose_timescan_rgba_stable_series_has_no_blue():
    """A scene that never changes has std ~ 0, so the variability (blue)
    channel stays dark even where mean/max are bright."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_timescan_rgba

    band = np.linspace(1.0, 100.0, 16, dtype="float32").reshape(4, 4)
    rgba = _compose_timescan_rgba([band, band, band], percentile=(0, 100))

    assert rgba.shape == (4, 4, 4)
    assert rgba.dtype.name == "uint8"
    assert (rgba[..., 2] == 0).all()  # blue (std) channel dark everywhere
    assert (rgba[..., 3] == 255).all()


def test_compose_timescan_rgba_variable_pixel_lights_up_blue():
    """The pixel that swings most across the series gets the strongest blue
    (highest temporal std); a steady pixel does not."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_timescan_rgba

    # Pixel (0,0) flickers bright/dark; pixel (1,1) holds steady. Other pixels
    # give the per-channel percentile stretch a spread to work against.
    t1 = np.array([[100.0, 10.0, 20.0], [30.0, 50.0, 40.0]], dtype="float32")
    t2 = np.array([[1.0, 12.0, 22.0], [32.0, 50.0, 42.0]], dtype="float32")
    t3 = np.array([[100.0, 14.0, 24.0], [34.0, 50.0, 44.0]], dtype="float32")
    rgba = _compose_timescan_rgba([t1, t2, t3], percentile=(0, 100))

    flicker_blue = int(rgba[0, 0, 2])
    steady_blue = int(rgba[1, 1, 2])
    assert flicker_blue > steady_blue
    assert flicker_blue == 255  # the most variable pixel tops the std stretch


def test_compose_timescan_rgba_invalid_pixels_propagate():
    """A pixel invalid on any one pass is transparent in the composite."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_timescan_rgba

    t1 = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    t2 = np.array([[np.nan, 2.0], [3.0, 4.0]], dtype="float32")
    t3 = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    rgba = _compose_timescan_rgba([t1, t2, t3])
    assert rgba[0, 0, 3] == 0  # invalid in t2 -> transparent
    assert rgba[1, 1, 3] == 255  # valid in all -> opaque


def test_compose_timescan_rgba_requires_three_bands():
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_timescan_rgba

    band = np.ones((2, 2), dtype="float32")
    with pytest.raises(ValueError, match="at least 3"):
        _compose_timescan_rgba([band, band])


def test_compose_timescan_rgba_db_keeps_steady_pixel_dark():
    """In the dB domain a steady pixel still has ~zero variability."""
    np = pytest.importorskip("numpy")
    from umbra_py.viz import _compose_timescan_rgba

    t1 = np.array([[100.0, 10.0], [50.0, 5.0]], dtype="float32")
    t2 = np.array([[1.0, 10.0], [50.0, 5.0]], dtype="float32")
    t3 = np.array([[100.0, 10.0], [50.0, 5.0]], dtype="float32")
    rgba = _compose_timescan_rgba([t1, t2, t3], percentile=(0, 100), db=True)
    # Steady pixels (anything but (0,0)) carry no variability.
    assert rgba[0, 1, 2] == 0
    assert rgba[1, 0, 2] == 0
    assert rgba[0, 0, 2] == 255  # the flickering pixel tops the std stretch


def test_timescan_composite_returns_pil_image(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    t1 = np.linspace(1.0, 100.0, 12, dtype="float32").reshape(3, 4)
    t2 = t1[::-1].copy()
    t3 = t1.copy()
    monkeypatch.setattr(viz_mod, "_coregister_bands", lambda *a, **k: ([t1, t2, t3], (0, 0, 1, 1)))

    items = [UmbraItem(id=c, bbox=(0.0, 0.0, 1.0, 1.0)) for c in "abc"]
    img = viz_mod.timescan_composite(items)
    assert img.size == (4, 3)  # PIL is (width, height)
    assert img.mode == "RGBA"


def test_timescan_composite_rejects_too_few_items(monkeypatch):
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(
        viz_mod,
        "_coregister_bands",
        lambda *a, **k: pytest.fail("should not co-register a bad item count"),
    )
    items = [UmbraItem(id=c, bbox=(0.0, 0.0, 1.0, 1.0)) for c in "ab"]
    with pytest.raises(ValueError, match="at least 3"):
        viz_mod.timescan_composite(items)


def test_save_timescan_composite_writes_png(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from umbra_py import viz as viz_mod

    t1 = np.arange(1, 17, dtype="float32").reshape(4, 4)
    monkeypatch.setattr(
        viz_mod,
        "_coregister_bands",
        lambda *a, **k: ([t1, t1[::-1].copy(), t1.copy()], (0, 0, 1, 1)),
    )

    items = [UmbraItem(id=c, bbox=(0.0, 0.0, 1.0, 1.0)) for c in "abc"]
    out = viz_mod.save_timescan_composite(items, tmp_path / "timescan.png")
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_cli_timescan_writes_image(monkeypatch, tmp_path, sample_item_dict):
    """End-to-end: `umbra timescan <url> <url> <url>` writes a PNG."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import viz as viz_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    t1 = np.arange(1, 65, dtype="float32").reshape(8, 8)
    monkeypatch.setattr(
        viz_mod,
        "_coregister_bands",
        lambda *a, **k: ([t1, t1[::-1].copy(), t1.copy()], (0, 0, 1, 1)),
    )

    out = tmp_path / "timescan.png"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "timescan",
            "http://example/a.json",
            "http://example/b.json",
            "http://example/c.json",
            "--out",
            str(out),
            "--db",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote timescan composite" in result.output


def test_cli_timescan_rejects_too_few_urls(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "timescan",
            "http://example/a.json",
            "http://example/b.json",
            "--out",
            str(tmp_path / "x.png"),
        ],
    )
    assert result.exit_code != 0
    assert "3 or more" in result.output


def test_cli_timescan_rejects_urls_and_search_mode(monkeypatch, tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(cli_mod, "get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "timescan",
            "http://example/a.json",
            "http://example/b.json",
            "http://example/c.json",
            "--area",
            "Centerfield",
            "--out",
            str(tmp_path / "x.png"),
        ],
    )
    assert result.exit_code != 0
    assert "not both" in result.output
