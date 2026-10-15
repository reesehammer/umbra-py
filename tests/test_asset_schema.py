"""Asset schema v2: stable product keys instead of filenames.

The published API and stac-geoparquet used to key ``assets`` by the on-disk
filename, which made the parquet ``assets`` struct one field per file (~79k).
These tests lock the mapping, the collision suffix, the parquet schema, the
API document, and the lookups that still have to find a file by product type
or by the old filename.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from umbra_py.constants import ASSET_SCHEMA_FIELD, ASSET_SCHEMA_VERSION
from umbra_py.exceptions import AssetNotFoundError
from umbra_py.export import _export_doc
from umbra_py.index import CatalogIndex, resolve_asset_name
from umbra_py.models import UmbraItem, _normalize_assets
from umbra_py.serve import collection, item_to_stac

_STEM = "2025-07-15-06-10-12_UMBRA-08"
_BUCKET = "https://umbra-open-data-catalog.s3.us-west-2.amazonaws.com"
_HREF = (
    f"{_BUCKET}/sar-data/task-data/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/"
    f"{_STEM}/{_STEM}.stac.v2.json"
)


def _file(name: str, *, media: str = "application/octet-stream") -> dict:
    return {"href": f"{_BUCKET}/sar-data/tasks/Site/{_STEM}/{name}", "type": media}


def _polygon():
    return {
        "type": "Polygon",
        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
    }


def test_filename_keys_map_to_the_stable_set():
    stem = _STEM
    assets = {
        f"{stem}_GEC.tif": _file(f"{stem}_GEC.tif", media="image/tiff"),
        f"{stem}_CSI.tif": _file(f"{stem}_CSI.tif", media="image/tiff"),
        f"{stem}_SIDD.nitf": _file(f"{stem}_SIDD.nitf"),
        f"{stem}_CSI-SIDD.nitf": _file(f"{stem}_CSI-SIDD.nitf"),
        f"{stem}_SICD.nitf": _file(f"{stem}_SICD.nitf"),
        f"{stem}_CPHD.cphd": _file(f"{stem}_CPHD.cphd"),
        f"{stem}_METADATA.json": _file(f"{stem}_METADATA.json", media="application/json"),
        f"{stem}_STAC-METADATA.json": _file(f"{stem}_STAC-METADATA.json", media="application/json"),
        f"{stem}_DI-GIF.gif": _file(f"{stem}_DI-GIF.gif", media="image/gif"),
        f"{stem}.tif": _file(f"{stem}.tif", media="image/tiff"),
        f"{stem}.cphd": _file(f"{stem}.cphd"),
    }
    # A second GEC-shaped name sorts after the canonical one and must not
    # take the bare key. The bare ``<stem>.tif`` is also a GEC.
    out = _normalize_assets(assets)
    assert list(out) == [
        "gec",
        "gec_2",
        "csi",
        "sidd",
        "csi_sidd",
        "sicd",
        "cphd",
        "cphd_2",
        "metadata",
        "stac_metadata",
        "di_gif",
    ]
    assert out["gec"]["umbra-py:filename"] == f"{stem}_GEC.tif"
    assert out["gec"]["title"] == f"{stem}_GEC.tif"
    assert out["gec_2"]["umbra-py:filename"] == f"{stem}.tif"
    assert out["cphd"]["umbra-py:filename"] == f"{stem}_CPHD.cphd"
    assert out["cphd_2"]["umbra-py:filename"] == f"{stem}.cphd"
    assert out["di_gif"]["umbra-py:filename"].endswith("_DI-GIF.gif")
    # The href is the public URL the catalog walk already stored.
    assert out["sicd"]["href"].endswith(f"{stem}_SICD.nitf")


def test_collision_suffix_is_deterministic_and_keeps_a_human_title():
    stem = "2024-01-01-00-00-00_UMBRA-04"
    first = f"{stem}_GEC.tif"
    second = f"{stem}_GEC_copy.tif"
    # Insertion order is the opposite of filename order. The suffix must
    # follow the filename, not dict order.
    out = _normalize_assets(
        {
            second: {"href": "https://example.com/b.tif", "title": "keep me", "type": "image/tiff"},
            first: {"href": "https://example.com/a.tif", "type": "image/tiff"},
        }
    )
    assert list(out) == ["gec", "gec_2"]
    assert out["gec"]["umbra-py:filename"] == first
    assert out["gec"]["title"] == first
    assert out["gec_2"]["umbra-py:filename"] == second
    assert out["gec_2"]["title"] == "keep me"
    again = _normalize_assets(out)
    assert list(again) == ["gec", "gec_2"]
    assert again["gec_2"]["title"] == "keep me"
    assert again["gec"]["umbra-py:filename"] == first


def test_unknown_extension_collapses_instead_of_keeping_the_filename():
    name = f"{_STEM}_NOTES.xml"
    out = _normalize_assets({name: {"href": f"https://example.com/{name}"}})
    assert list(out) == ["file_xml"]
    assert out["file_xml"]["umbra-py:filename"] == name


def test_canonical_uppercase_keys_lowercase_and_keep_their_title():
    """Umbra's own sidecar uses ``GEC`` / ``metadata``, not a filename."""
    out = _normalize_assets(
        {
            "GEC": {
                "href": f"https://example.com/{_STEM}_GEC.tif",
                "title": "GEC",
                "type": "image/tiff; application=geotiff",
            },
            "metadata": {
                "href": f"https://example.com/{_STEM}_METADATA.json",
                "title": "metadata",
                "type": "application/json",
            },
        }
    )
    assert set(out) == {"gec", "metadata"}
    assert out["gec"]["title"] == "GEC"
    assert out["gec"]["umbra-py:filename"] == f"{_STEM}_GEC.tif"
    assert out["metadata"]["umbra-py:filename"].endswith("_METADATA.json")


def test_asset_href_accepts_product_stable_key_and_filename():
    """Download selection still finds the file after the key changes.

    The href is an ``s3://`` processing URL, so the public name has to be
    rebuilt from ``umbra-py:filename`` (a v1 ``*_MM.cphd`` whose timestamp is
    not the sidecar stem) rather than from the key ``cphd``.
    """
    sidecar = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/task-data/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/"
        "2026-02-20-03-03-38_UMBRA-07/2026-02-20-03-03-38_UMBRA-07.stac.v2.json"
    )
    v1_name = "2026-02-20-03-03-29_UMBRA-07_MM.cphd"
    item = UmbraItem.from_dict(
        {
            "id": "x",
            "properties": {},
            "assets": _normalize_assets(
                {
                    v1_name: {
                        "href": f"s3://prod-prod-processed-sar-data/2026-02-20/abc/{v1_name}",
                        "type": "application/octet-stream",
                    }
                }
            ),
        },
        href=sidecar,
    )
    expect = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/task-data/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/"
        "2026-02-20-03-03-38_UMBRA-07/2026-02-20-03-03-38_UMBRA-07_CPHD.cphd"
    )
    assert item.asset_map["CPHD"] == "cphd"
    assert item.asset_href("CPHD") == expect
    assert item.asset_href("cphd") == expect
    assert item.asset_href(v1_name) == expect
    assert item.has_asset("CPHD")
    assert item.has_asset(v1_name)
    assert not item.has_asset("GEC")
    with pytest.raises(AssetNotFoundError):
        item.asset_href("GEC")


def test_primary_gec_wins_over_the_collision_suffix():
    stem = _STEM
    bare = f"{stem}.tif"
    canonical = f"{stem}_GEC.tif"
    item = UmbraItem.from_dict(
        {
            "id": "x",
            "assets": _normalize_assets(
                {
                    bare: {"href": "https://example.com/bare.tif", "type": "image/tiff"},
                    canonical: {
                        "href": "https://example.com/gec.tif",
                        "type": "image/tiff; application=geotiff",
                    },
                }
            ),
        },
        href=_HREF,
    )
    assert item.asset_href("GEC") == "https://example.com/gec.tif"
    assert item.asset_href("gec_2").endswith("bare.tif")
    assert item.available_assets == ["GEC"]


def test_sidd_download_prefers_the_non_csi_file():
    stem = _STEM
    item = UmbraItem.from_dict(
        {
            "id": "x",
            "assets": _normalize_assets(
                {
                    f"{stem}_CSI-SIDD.nitf": {"href": "https://example.com/csi-sidd.nitf"},
                    f"{stem}_SIDD.nitf": {"href": "https://example.com/sidd.nitf"},
                }
            ),
        },
        href=_HREF,
    )
    assert item.asset_href("SIDD") == "https://example.com/sidd.nitf"
    assert item.asset_href("csi_sidd").endswith("csi-sidd.nitf")


def _indexed_item(keys: dict[str, dict]) -> UmbraItem:
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "item-1",
        "bbox": [0, 0, 1, 1],
        "geometry": _polygon(),
        "properties": {
            "datetime": "2025-07-15T06:10:12Z",
            "umbra-py:products": ["GEC", "CPHD"],
        },
        "assets": keys,
    }
    return UmbraItem.from_dict(doc, href=_HREF)


def test_filename_lookup_works_when_the_key_is_not_the_filename(tmp_path):
    stem = _STEM
    filename = f"{stem}_GEC.tif"
    item = _indexed_item(
        _normalize_assets(
            {
                filename: {
                    "href": f"{_BUCKET}/sar-data/tasks/Site/{stem}/{filename}",
                    "type": "image/tiff",
                }
            }
        )
    )
    assert "gec" in item.assets
    assert filename not in item.assets
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(item)
        idx.commit()
        found = idx.find_by_asset_name(filename)
        by_stem = idx.find_by_asset_name(stem)
    assert [hit.id for hit in found] == ["item-1"]
    assert [hit.id for hit in by_stem] == ["item-1"]
    assert resolve_asset_name(filename, path=db).asset_href("GEC").endswith(filename)


def test_collection_and_item_documents_record_schema_v2():
    coll = collection("http://localhost:8000")
    assert coll[ASSET_SCHEMA_FIELD] == ASSET_SCHEMA_VERSION
    filename = f"{_STEM}_CPHD.cphd"
    item = _indexed_item({filename: {"href": f"https://example.com/{filename}"}})
    feature = item_to_stac(item, "http://localhost:8000")
    assert set(feature["assets"]) == {"cphd"}
    assert feature["assets"]["cphd"]["umbra-py:filename"] == filename
    assert feature["assets"]["cphd"]["title"] == filename
    assert feature["properties"][ASSET_SCHEMA_FIELD] == ASSET_SCHEMA_VERSION
    # Serve rewrites a copy. The indexed document keeps the filename key
    # until the next ``umbra index build``.
    assert filename in item.assets
    assert ASSET_SCHEMA_FIELD not in item.raw["properties"]


def test_export_doc_normalizes_without_mutating_the_item():
    filename = f"{_STEM}_GEC.tif"
    item = _indexed_item(
        {filename: {"href": f"https://example.com/{filename}", "type": "image/tiff"}}
    )
    item.place = "Reykjavík, Iceland"
    item.published = datetime(2025, 7, 16, tzinfo=timezone.utc)
    doc = _export_doc(item)
    assert set(doc["assets"]) == {"gec"}
    assert doc["assets"]["gec"]["umbra-py:filename"] == filename
    assert doc["properties"][ASSET_SCHEMA_FIELD] == ASSET_SCHEMA_VERSION
    assert doc["properties"]["umbra:place"] == "Reykjavík, Iceland"
    assert doc["properties"]["umbra-py:published"] == "2025-07-16T00:00:00Z"
    assert filename in item.raw["assets"]
    assert ASSET_SCHEMA_FIELD not in item.raw["properties"]


def test_catalog_walk_stores_v2_keys_and_matching_products():
    from umbra_py.catalog import UmbraCatalog

    stem = _STEM
    prefix = f"sar-data/tasks/Site/{stem}/"
    files = [
        f"{prefix}{stem}.stac.v2.json",
        f"{prefix}{stem}_GEC.tif",
        f"{prefix}{stem}_SICD.nitf",
        f"{prefix}{stem}_METADATA.json",
    ]
    doc = {
        "id": "walked",
        "bbox": [0, 0, 1, 1],
        "geometry": _polygon(),
        "properties": {"datetime": "2025-07-15T06:10:12Z"},
    }
    item = UmbraCatalog()._item_from_sidecar(
        doc, prefix, files, f"https://example.com/{stem}.stac.v2.json"
    )
    assert item is not None
    assert set(item.assets) == {"gec", "sicd", "metadata"}
    assert item.properties["umbra-py:products"] == ["GEC", "SICD"]
    assert item.properties[ASSET_SCHEMA_FIELD] == ASSET_SCHEMA_VERSION
    assert item.asset_href("SICD").endswith(f"{stem}_SICD.nitf")
    assert item.raw["assets"]["metadata"]["umbra-py:filename"].endswith("_METADATA.json")


def test_api_search_and_items_serve_v2_keys(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve

    filename = f"{_STEM}_CPHD.cphd"
    gec = f"{_STEM}_GEC.tif"
    item = _indexed_item(
        {
            filename: {"href": f"https://example.com/{filename}"},
            gec: {"href": f"https://example.com/{gec}", "type": "image/tiff"},
        }
    )
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        idx.add(item)
        idx.commit()
    client = TestClient(serve.build_app(path, artifacts=False))
    coll = client.get("/collections/umbra-open-data").json()
    assert coll[ASSET_SCHEMA_FIELD] == 2
    fetched = client.get("/collections/umbra-open-data/items/item-1").json()
    searched = client.get("/search?limit=5").json()["features"][0]
    for feature in (fetched, searched):
        assert set(feature["assets"]) == {"cphd", "gec"}
        assert feature["assets"]["cphd"]["umbra-py:filename"] == filename
        assert feature["properties"][ASSET_SCHEMA_FIELD] == 2
        assert feature["properties"]["umbra-py:products"] == ["GEC", "CPHD"]


def test_parquet_schema_uses_the_fixed_asset_keys(tmp_path):
    pytest.importorskip("stac_geoparquet")
    pq = pytest.importorskip("pyarrow.parquet")
    from umbra_py.export import export_geoparquet

    stem_a = "2025-07-15-06-10-12_UMBRA-08"
    stem_b = "2024-09-14-05-16-06_UMBRA-07"

    def item(item_id: str, stem: str, names: list[str]) -> UmbraItem:
        assets = {
            name: {"href": f"https://example.com/{name}", "type": "application/octet-stream"}
            for name in names
        }
        return UmbraItem.from_dict(
            {
                "type": "Feature",
                "stac_version": "1.0.0",
                "id": item_id,
                "bbox": [0, 0, 1, 1],
                "geometry": _polygon(),
                "properties": {"datetime": "2025-07-15T06:10:12Z"},
                "assets": assets,
            },
            href=f"https://example.com/{stem}/{stem}.stac.v2.json",
        )

    items = [
        item(
            "a",
            stem_a,
            [f"{stem_a}_GEC.tif", f"{stem_a}.tif", f"{stem_a}_CPHD.cphd"],
        ),
        item(
            "b",
            stem_b,
            [
                f"{stem_b}_SICD.nitf",
                f"{stem_b}_DI-GIF.gif",
                f"{stem_b}_STAC-METADATA.json",
                f"{stem_b}_METADATA.json",
                f"{stem_b}_CSI-SIDD.nitf",
                f"{stem_b}_CSI.tif",
                f"{stem_b}_SIDD.nitf",
            ],
        ),
    ]
    out = tmp_path / "catalog.parquet"
    assert export_geoparquet(items, out) == 2
    schema = pq.read_schema(out)
    asset_names = set(schema.field("assets").type.names)
    assert asset_names == {
        "gec",
        "gec_2",
        "csi",
        "sidd",
        "csi_sidd",
        "sicd",
        "cphd",
        "metadata",
        "stac_metadata",
        "di_gif",
    }
    # No filename survived as a struct field.
    assert not any(name[0].isdigit() for name in asset_names)
    meta = json.loads(schema.metadata[b"stac-geoparquet"])
    recorded = meta["collections"]["umbra-open-data"][ASSET_SCHEMA_FIELD]
    assert recorded == ASSET_SCHEMA_VERSION
