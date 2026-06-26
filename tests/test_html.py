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


def test_standalone_gallery_html_is_a_full_document(sample_item_dict):
    from umbra_py._html import standalone_gallery_html

    item = UmbraItem.from_dict(sample_item_dict, href="http://example/item.json")
    other = UmbraItem(id="second-item")
    html = standalone_gallery_html(
        [item, other],
        thumbnails={0: "data:image/png;base64,ZZZ"},
        subtitle="rome",
    )
    assert html.startswith("<!doctype html>")
    assert "</html>" in html.strip().splitlines()[-1]
    # Thumbnail embedded for the first item; both ids present.
    assert 'src="data:image/png;base64,ZZZ"' in html
    assert item.id in html
    assert "second-item" in html
    # Each tile links to its STAC item, plus the header context + attribution.
    assert 'href="http://example/item.json"' in html
    assert "rome" in html
    assert "Umbra open data" in html  # ATTRIBUTION footer


def test_standalone_gallery_tile_falls_back_to_footprint(sample_item_dict):
    from umbra_py._html import standalone_gallery_html

    item = UmbraItem.from_dict(sample_item_dict)
    # No thumbnail supplied: the tile must still render, drawing the footprint
    # sketch from geometry instead of a SAR image.
    html = standalone_gallery_html([item], thumbnails={})
    assert "data:image" not in html
    assert "<svg" in html
    assert "1 acquisition" in html  # singular


def test_standalone_gallery_html_escapes_id():
    from umbra_py._html import standalone_gallery_html

    item = UmbraItem(id="<script>evil</script>")
    html = standalone_gallery_html([item])
    assert "<script>evil" not in html
    assert "&lt;script&gt;" in html


def test_standalone_gallery_html_exposes_asset_and_stac_urls(sample_item_dict):
    from umbra_py._html import standalone_gallery_html

    item = UmbraItem.from_dict(sample_item_dict, href="http://example/item.stac.json")
    html = standalone_gallery_html([item])
    # The rendered asset's direct download URL is shown copyably...
    assert item.asset_href("GEC") in html
    assert "_GEC.tif" in html
    # ...alongside the STAC item URL for `umbra info|download|quicklook|load`.
    assert "http://example/item.stac.json" in html
    assert "user-select:all" in html  # click-to-select, no JS


def test_standalone_gallery_html_asset_url_follows_asset_arg(sample_item_dict):
    from umbra_py._html import standalone_gallery_html

    item = UmbraItem.from_dict(sample_item_dict)
    html = standalone_gallery_html([item], asset="SICD")
    assert item.asset_href("SICD") in html  # the SICD NITF, not the GEC tif
    assert "_SICD.nitf" in html


def test_standalone_gallery_tile_omits_url_panel_when_unresolvable():
    from umbra_py._html import standalone_gallery_html

    # No assets and no href: nothing to expose, and the page must still render.
    item = UmbraItem(id="bare", bbox=(0.0, 0.0, 1.0, 1.0))
    html = standalone_gallery_html([item])
    assert "bare" in html
    assert "<details" not in html
