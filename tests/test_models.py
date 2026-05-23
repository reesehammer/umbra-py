import pytest

from umbra_py.exceptions import AssetNotFoundError
from umbra_py.models import UmbraItem, _bbox_from_geometry


def test_from_dict_parses_real_item(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="http://example/item.json")
    assert item.id == sample_item_dict["id"]
    assert item.product_type == "GEC"
    assert item.platform == "UMBRA_04"
    assert item.polarizations == ["VV"]
    assert item.instrument_mode == "SPOTLIGHT"
    assert item.href == "http://example/item.json"


def test_available_assets_order(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    # PRODUCT_ASSETS order: GEC, SIDD, SICD, CPHD
    assert item.available_assets == ["GEC", "SIDD", "SICD", "CPHD"]


def test_asset_href_and_missing(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    assert item.asset_href("GEC").endswith("_GEC.tif")
    with pytest.raises(AssetNotFoundError):
        item.asset_href("NOPE")


def test_bbox_from_3d_geometry():
    geom = {
        "type": "Polygon",
        "coordinates": [[[10.0, 50.0, -1.0], [12.0, 52.0, -2.0], [11.0, 51.0, -1.5]]],
    }
    assert _bbox_from_geometry(geom) == (10.0, 50.0, 12.0, 52.0)


def test_intersects_bbox():
    item = UmbraItem(id="x", bbox=(0.0, 0.0, 1.0, 1.0))
    assert item.intersects_bbox((0.5, 0.5, 2.0, 2.0))
    assert not item.intersects_bbox((5.0, 5.0, 6.0, 6.0))


def test_summary_is_readable(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    text = item.summary()
    assert item.id in text
    assert "GEC" in text


def test_metadata_summary_keys(sample_item_dict):
    summary = UmbraItem.from_dict(sample_item_dict).metadata_summary()
    assert set(summary) >= {"id", "datetime", "product_type", "bbox", "available_assets"}
