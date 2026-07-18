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


def test_to_llm_context_has_explanations_and_license(sample_item_dict):
    ctx = UmbraItem.from_dict(sample_item_dict, href="http://ex/item.json").to_llm_context()
    assert ctx["id"] == sample_item_dict["id"]
    assert ctx["license"] == "CC-BY-4.0"
    assert "CC BY 4.0" in ctx["attribution"]
    # The polarization caveat travels with the polarizations.
    assert ctx["polarizations"] == ["VV"]
    assert "polarization" in ctx["polarization_caveat"].lower()
    # Each present product carries a type, a non-empty explanation, and a URL.
    products = {p["type"]: p for p in ctx["products"]}
    assert "GEC" in products
    assert products["GEC"]["explanation"]
    assert products["GEC"]["url"].endswith("_GEC.tif")


def test_to_llm_context_prefers_baked_place(sample_item_dict):
    """A `CatalogIndex` search bakes a reverse-geocoded label onto `.place`;
    the context card should surface it so an agent reasons about a real place
    name, not the task codename."""
    item = UmbraItem.from_dict(sample_item_dict, href="http://ex/item.json")
    # Without a baked label the card falls back to the task codename.
    assert item.place is None
    assert item.to_llm_context()["place"] == item.task
    # With one, the card prefers it.
    item.place = "Reykjavík, Iceland"
    assert item.to_llm_context()["place"] == "Reykjavík, Iceland"


def test_geo_interface_item_is_a_feature(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict)
    geo = item.__geo_interface__
    assert geo["type"] == "Feature"
    assert geo == item.to_geojson()


def test_geo_interface_collection_is_a_featurecollection(sample_item_dict):
    from umbra_py.models import ItemCollection

    coll = ItemCollection([UmbraItem.from_dict(sample_item_dict)])
    geo = coll.__geo_interface__
    assert geo["type"] == "FeatureCollection"
    assert len(geo["features"]) == 1


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


def test_plain_image_tiff_asset_is_classified_as_geotiff():
    """A GeoTIFF that declares a plain ``image/tiff`` media type (no "geotiff"
    profile substring) must still classify as GEC via its ``.tif`` key.

    Regression for the dead ``"tif" in name`` branch: ``name`` is upper-cased,
    so the lowercase substring never matched and such an asset was dropped.
    """
    item = UmbraItem.from_dict(
        {
            "id": "plain-tiff",
            "properties": {"umbra:task_id": "task-abc"},
            "assets": {
                "2025-06-22-23-57-52_UMBRA-10_MM.tif": {
                    "href": "",
                    "type": "image/tiff",  # plain: no "geotiff" profile
                    "title": "GEC",
                },
            },
        }
    )
    assert item.asset_map.get("GEC") == "2025-06-22-23-57-52_UMBRA-10_MM.tif"
    assert "GEC" in item.available_assets


def test_task_reads_decoded_label_from_href():
    sidecar = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/tasks/Bingham%20Copper%20Mine/658ce57e/"
        "2024-10-24-05-13-36_UMBRA-07/2024-10-24-05-13-36_UMBRA-07.stac.v2.json"
    )
    item = UmbraItem.from_dict({"id": "x"}, href=sidecar)
    assert item.task == "Bingham Copper Mine"


def test_task_falls_back_to_task_id_property():
    item = UmbraItem.from_dict(
        {"id": "x", "properties": {"umbra:task_id": "658ce57e"}},
        href="http://example/item.json",  # no /sar-data/tasks/ component
    )
    assert item.task == "658ce57e"


def test_task_is_none_when_unknown():
    assert UmbraItem(id="x").task is None
