"""Tests for the Jupyter ``_repr_html_`` rendering (offline, no extras)."""

import umbra_py.models as models_mod
from umbra_py.models import ItemCollection, UmbraItem


def test_item_repr_html_has_metadata_and_footprint(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="http://example/item.json")
    html = item._repr_html_()
    assert item.id in html
    assert "GEC" in html  # product type / asset
    assert "<table" in html
    assert "<svg" in html  # footprint sketch drawn from geometry
    assert 'href="http://example/item.json"' in html


def test_item_repr_html_without_geometry_omits_svg():
    item = UmbraItem(id="no-geom")
    html = item._repr_html_()
    assert "no-geom" in html
    assert "<svg" not in html  # nothing to draw, but must not raise


def test_item_repr_html_escapes_id():
    item = UmbraItem(id="<script>evil</script>")
    html = item._repr_html_()
    assert "<script>evil" not in html
    assert "&lt;script&gt;" in html


def test_collection_is_a_list(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    coll = ItemCollection([item, item])
    assert isinstance(coll, list)
    assert len(coll) == 2
    assert coll[0] is item


def test_collection_repr_html_renders_all_items(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    other = UmbraItem(id="second-item")
    coll = ItemCollection([item, other])
    html = coll._repr_html_()
    assert item.id in html
    assert "second-item" in html
    assert "2 items" in html


def test_collection_default_does_not_fetch_thumbnails(sample_item_dict, monkeypatch):
    # The offline default must never reach for the viz thumbnail helper.
    import umbra_py.viz as viz

    def boom(*a, **k):  # pragma: no cover - should not be called
        raise AssertionError("thumbnails fetched without opt-in")

    monkeypatch.setattr(viz, "_thumbnail_data_uri", boom)
    coll = ItemCollection([UmbraItem.from_dict(sample_item_dict)])
    coll._repr_html_()  # must not raise


def test_collection_thumbnails_embedded_when_opted_in(sample_item_dict, monkeypatch):
    import umbra_py.viz as viz

    monkeypatch.setattr(viz, "_thumbnail_data_uri", lambda *a, **k: "data:image/png;base64,ZZZ")
    coll = ItemCollection([UmbraItem.from_dict(sample_item_dict)], thumbnails=True)
    html = coll._repr_html_()
    assert 'src="data:image/png;base64,ZZZ"' in html


def test_collection_thumbnail_failure_falls_back(sample_item_dict, monkeypatch):
    import umbra_py.viz as viz

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(viz, "_thumbnail_data_uri", boom)
    coll = ItemCollection([UmbraItem.from_dict(sample_item_dict)], thumbnails=True)
    html = coll._repr_html_()  # must not raise despite the failure
    assert sample_item_dict["id"] in html
    assert "data:image" not in html  # fell back to footprint card


def test_footprint_svg_handles_zero_area_bbox():
    # A degenerate (point) footprint has zero span; we should bail, not divide
    # by zero.
    item = UmbraItem(
        id="point",
        geometry={"type": "Polygon", "coordinates": [[[1.0, 2.0], [1.0, 2.0]]]},
        bbox=(1.0, 2.0, 1.0, 2.0),
    )
    assert models_mod.UmbraItem._repr_html_(item)  # renders without error
