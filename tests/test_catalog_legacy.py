"""Legacy layouts the crawler must index (PRD P1-1 coverage parity).

Three shapes found in the open-data bucket besides the v2 acquisition directory:
legacy ``_METADATA.json``-only acquisition directories (2023 to 2024 named
tasks), the flat legacy layout under ``tasks/ad hoc/<site>/`` with no
acquisition directory, and v2 sidecars misplaced into a sibling
``<stem>.stac.v2/`` directory.
"""

from __future__ import annotations

import pytest

from umbra_py.catalog import (
    UmbraCatalog,
    _acquisition_group,
    _pick_sidecar,
    stac_from_legacy_metadata,
)

STEM = "2023-07-18-02-30-32_UMBRA-04"


def _legacy_doc(collect_id: str = "5ba5b849-2f14-4936-a0e9-4482afe45284") -> dict:
    return {
        "version": "1.0.0",
        "vendor": "Umbra Space",
        "imagingMode": "SPOTLIGHT",
        "productSku": "UMB-SPOTLIGHT-50-1",
        "umbraSatelliteName": "UMBRA_04",
        "collects": [
            {
                "id": collect_id,
                "taskId": "2e00bde3-c6d1-4230-aca0-11cd51c9c998",
                "startAtUTC": "2023-07-18T02:30:33+00:00",
                "endAtUTC": "2023-07-18T02:30:36.374803+00:00",
                "radarBand": "X",
                "radarCenterFrequencyHz": 9600001094.9,
                "polarizations": ["VV"],
                "angleAzimuthDegrees": 122.1,
                "angleGrazingDegrees": 42.6,
                "angleIncidenceDegrees": 47.4,
                "slantRangeMeters": 743643.9,
                "satelliteTrack": "ASCENDING",
                "observationDirection": "LEFT",
                "footprintPolygonLla": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-79.60, 8.95, 10.0],
                            [-79.55, 8.95, 10.0],
                            [-79.55, 9.00, 10.0],
                            [-79.60, 9.00, 10.0],
                            [-79.60, 8.95, 10.0],
                        ]
                    ],
                },
                "maxGroundResolution": {"azimuthMeters": 0.55, "rangeMeters": 0.54},
            }
        ],
        "derivedProducts": {
            "GEC": [{"groundResolution": {"azimuthMeters": 0.71, "rangeMeters": 0.70}}]
        },
    }


@pytest.mark.parametrize(
    "rel,expected",
    [
        (f"uuid/{STEM}/{STEM}_CPHD.cphd", (f"uuid/{STEM}/", STEM)),
        (f"{STEM}/{STEM}.stac.v2.json", (f"{STEM}/", STEM)),
        # misplaced v2 sidecar folds into its acquisition directory
        (f"uuid/{STEM}.stac.v2/{STEM}.stac.v2.stac.v2.json", (f"uuid/{STEM}/", STEM)),
        # flat legacy layout groups on the file-name stem
        (f"Aswan Dam, Egypt/{STEM}.tif", (f"Aswan Dam, Egypt/{STEM}", STEM)),
        (f"Aswan Dam, Egypt/{STEM}_METADATA.json", (f"Aswan Dam, Egypt/{STEM}", STEM)),
        (f"site/uuid/{STEM}_SICD.nitf", (f"site/uuid/{STEM}", STEM)),
        ("readme.txt", None),
        ("site/notes.json", None),
    ],
)
def test_acquisition_group(rel, expected):
    assert _acquisition_group(rel) == expected


def test_pick_sidecar_prefers_v2_over_legacy():
    keys = [f"a/{STEM}_METADATA.json", f"a/{STEM}.stac.v2.json", f"a/{STEM}_GEC.tif"]
    assert _pick_sidecar(keys) == f"a/{STEM}.stac.v2.json"
    assert _pick_sidecar(keys[::2]) == f"a/{STEM}_METADATA.json"
    assert _pick_sidecar([f"a/{STEM}_GEC.tif"]) is None


def test_stac_from_legacy_metadata_maps_search_fields():
    stac = stac_from_legacy_metadata(_legacy_doc())
    assert stac is not None
    props = stac["properties"]
    assert props["datetime"] == "2023-07-18T02:30:33+00:00"
    assert props["platform"] == "Umbra-04"
    # incidence is the incidence angle, never the grazing angle
    assert props["view:incidence_angle"] == 47.4
    assert props["umbra:grazing_angle_degrees"] == 42.6
    assert props["sar:polarizations"] == ["VV"]
    assert props["sar:resolution_range"] == 0.70
    assert props["sat:orbit_state"] == "ascending"
    assert props["umbra:collect_id"] == "5ba5b849-2f14-4936-a0e9-4482afe45284"
    # heights dropped, bbox from the footprint
    assert all(len(pt) == 2 for pt in stac["geometry"]["coordinates"][0])
    assert stac["bbox"] == [-79.60, 8.95, -79.55, 9.00]


def test_legacy_ids_are_deterministic_and_distinct():
    a = stac_from_legacy_metadata(_legacy_doc("one"))["id"]
    assert a == stac_from_legacy_metadata(_legacy_doc("one"))["id"]
    assert a != stac_from_legacy_metadata(_legacy_doc("two"))["id"]


@pytest.mark.parametrize("drop", ["collects", "startAtUTC", "footprintPolygonLla", "id"])
def test_stac_from_legacy_metadata_rejects_incomplete(drop):
    doc = _legacy_doc()
    if drop == "collects":
        doc["collects"] = []
    else:
        doc["collects"][0].pop(drop)
    assert stac_from_legacy_metadata(doc) is None


@pytest.fixture
def legacy_bucket(monkeypatch):
    v2_stem = "2024-11-01-17-16-12_UMBRA-06"
    misplaced = f"{v2_stem}.stac.v2/{v2_stem}.stac.v2.stac.v2.json"
    flat_stem = "2023-04-02-07-46-55_UMBRA-05"
    keys = {
        "sar-data/tasks/Panama Canal, Panama/": [
            f"sar-data/tasks/Panama Canal, Panama/uuid/{STEM}/{STEM}_CPHD.cphd",
            f"sar-data/tasks/Panama Canal, Panama/uuid/{STEM}/{STEM}_GEC.tif",
            f"sar-data/tasks/Panama Canal, Panama/uuid/{STEM}/{STEM}_METADATA.json",
            f"sar-data/tasks/Panama Canal, Panama/uuid/{STEM}/{STEM}_SICD.nitf",
        ],
        "sar-data/tasks/ad hoc/": [
            f"sar-data/tasks/ad hoc/Aswan Dam, Egypt/{flat_stem}.tif",
            f"sar-data/tasks/ad hoc/Aswan Dam, Egypt/{flat_stem}_METADATA.json",
            f"sar-data/tasks/ad hoc/Aswan Dam, Egypt/{flat_stem}_SIDD.nitf",
        ],
        "sar-data/tasks/Bingham Copper Mine/": [
            f"sar-data/tasks/Bingham Copper Mine/u2/{misplaced}",
            f"sar-data/tasks/Bingham Copper Mine/u2/{v2_stem}/{v2_stem}_GEC.tif",
            f"sar-data/tasks/Bingham Copper Mine/u2/{v2_stem}/{v2_stem}_METADATA.json",
        ],
    }
    v2_doc = {
        "id": "bingham-v2",
        "bbox": [0, 0, 1, 1],
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]},
        "properties": {"datetime": "2024-11-01T17:16:14Z"},
        "assets": {},
    }

    def fake_list(self, prefix):
        if prefix == "sar-data/tasks/":
            return (list(keys), [])
        return ([], [])

    def fake_stream(self, prefix):
        yield from keys[prefix]

    def fake_get(self, url):
        if url.endswith(".stac.v2.stac.v2.json"):
            return v2_doc
        if url.endswith("_METADATA.json"):
            doc = _legacy_doc(collect_id=url.rsplit("/", 1)[-1])
            return doc
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_list_prefix", fake_list)
    monkeypatch.setattr(UmbraCatalog, "_stream_keys", fake_stream)
    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    return UmbraCatalog()


def test_walker_indexes_every_legacy_layout(legacy_bucket):
    items = {i.task: i for i in legacy_bucket.search()}
    assert set(items) == {"Panama Canal, Panama", "ad hoc", "Bingham Copper Mine"}

    panama = items["Panama Canal, Panama"]
    assert panama.properties["umbra-py:layout"] == "legacy"
    assert panama.available_assets == ["GEC", "SICD", "CPHD"]
    assert panama.properties["umbra-py:cphd_sicd_pair"] is True
    assert panama.asset_href("CPHD").endswith(
        f"/Panama%20Canal%2C%20Panama/uuid/{STEM}/{STEM}_CPHD.cphd"
    )

    flat = items["ad hoc"]
    assert flat.properties["umbra-py:products"] == ["GEC", "SIDD"]
    assert flat.properties["umbra-py:cphd_sicd_pair"] is False

    bingham = items["Bingham Copper Mine"]
    assert bingham.id == "bingham-v2"
    assert bingham.properties["umbra-py:layout"] == "v2"
    assert bingham.available_assets == ["GEC"]


def test_legacy_items_respect_date_pruning(legacy_bucket):
    tasks = {i.task for i in legacy_bucket.search(start="2024-01-01")}
    assert tasks == {"Bingham Copper Mine"}
