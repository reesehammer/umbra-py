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
