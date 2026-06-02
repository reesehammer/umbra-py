import pytest

from umbra_py.exceptions import AssetNotFoundError
from umbra_py.models import UmbraItem, _bbox_from_geometry, _derive_data_url


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


@pytest.mark.parametrize(
    "key,disk_suffix",
    [
        ("2025-06-22-23-57-52_UMBRA-10_MM.tif", "_GEC.tif"),
        ("2025-06-22-23-57-52_UMBRA-10_CSI_MM.tif", "_CSI.tif"),
        ("2025-06-22-23-57-52_UMBRA-10_CSI_SIDD_MM.nitf", "_CSI-SIDD.nitf"),
        ("2025-06-22-23-57-52_UMBRA-10_SICD_MM.nitf", "_SICD.nitf"),
        ("2025-06-22-23-57-52_UMBRA-10_SIDD_MM.nitf", "_SIDD.nitf"),
        ("2025-06-22-23-57-52_UMBRA-10_MM.cphd", "_CPHD.cphd"),
    ],
)
def test_derive_data_url_maps_v1_suffixes(key, disk_suffix):
    url = _derive_data_url(key, task_id="task-abc")
    assert url is not None
    base = "2025-06-22-23-57-52_UMBRA-10"
    expected_tail = f"/sar-data/tasks/task-abc/{base}/{base}{disk_suffix}"
    assert url.endswith(expected_tail), url


def test_derive_data_url_returns_none_for_unrecognised_keys():
    assert _derive_data_url("something_METADATA.json", task_id="t") is None
    assert _derive_data_url("plain.txt", task_id="t") is None


def _new_style_item():
    return UmbraItem.from_dict(
        {
            "id": "demo",
            "geometry": None,
            "bbox": [0, 0, 1, 1],
            "properties": {
                "sar:product_type": "GEC",
                "umbra:task_id": "task-abc",
            },
            "assets": {
                "2025-06-22-23-57-52_UMBRA-10_MM.tif": {
                    "href": "",
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "title": "GEC",
                },
                "2025-06-22-23-57-52_UMBRA-10_SICD_MM.nitf": {
                    "href": "",
                    "type": "application/octet-stream",
                    "title": "SICD",
                },
            },
        }
    )


def test_asset_href_resolves_empty_href_via_task_id():
    item = _new_style_item()
    gec = item.asset_href("GEC")
    assert gec.startswith("https://s3.")
    assert "/sar-data/tasks/task-abc/" in gec
    assert gec.endswith("/2025-06-22-23-57-52_UMBRA-10/2025-06-22-23-57-52_UMBRA-10_GEC.tif")

    sicd = item.asset_href("SICD")
    assert sicd.endswith("/2025-06-22-23-57-52_UMBRA-10_SICD.nitf")


def test_asset_href_rewrites_private_s3_href_to_public_sidecar_sibling():
    """Umbra's published sidecars carry s3:// hrefs into a private bucket.
    Built straight from the STAC JSON (umbra info / download / quicklook),
    the item must still resolve a public, fetchable URL -- the sibling of the
    sidecar in the open bucket. Regression for the named-task case, where
    deriving from umbra:task_id alone points at a path that 404s."""
    sidecar = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/tasks/Bingham%20Copper%20Mine/658ce57e/"
        "2024-10-24-05-13-36_UMBRA-07/2024-10-24-05-13-36_UMBRA-07.stac.v2.json"
    )
    item = UmbraItem.from_dict(
        {
            "id": "x",
            "properties": {"umbra:task_id": "658ce57e"},
            "assets": {
                "2024-10-24-05-13-36_UMBRA-07_MM.tif": {
                    "href": (
                        "s3://prod-prod-processed-sar-data/2024-10-24/abc/"
                        "2024-10-24-05-13-36_UMBRA-07_MM.tif"
                    ),
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                }
            },
        },
        href=sidecar,
    )
    gec = item.asset_href("GEC")
    assert "prod-prod-processed-sar-data" not in gec
    assert gec == (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/tasks/Bingham%20Copper%20Mine/658ce57e/"
        "2024-10-24-05-13-36_UMBRA-07/2024-10-24-05-13-36_UMBRA-07_GEC.tif"
    )


def test_asset_href_falls_back_to_empty_without_task_id():
    # Same item shape but no umbra:task_id -> nothing we can derive.
    raw = _new_style_item().raw
    raw["properties"] = {"sar:product_type": "GEC"}
    item = UmbraItem.from_dict(raw)
    assert item.asset_href("GEC") == ""
