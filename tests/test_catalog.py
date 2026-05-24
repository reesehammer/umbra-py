from datetime import date

import pytest

from umbra_py.catalog import UmbraCatalog, _acq_date


@pytest.mark.parametrize(
    "name,expected",
    [
        ("sar-data/tasks/AIR/uuid/2025-12-06-07-52-28_UMBRA-10/", date(2025, 12, 6)),
        ("2024-01-15-10-00-00_UMBRA-04/", date(2024, 1, 15)),
        ("not-an-acquisition/", None),
        ("sar-data/tasks/AIR/", None),
        ("2024-13-40-99-99-99_BAD/", None),  # invalid date components
    ],
)
def test_acq_date(name, expected):
    assert _acq_date(name) == expected


# A minimal v2 STAC item document. We omit anything the walker doesn't need;
# UmbraItem.from_dict tolerates missing fields.
def _sidecar(item_id: str, dt: str, bbox: tuple) -> dict:
    return {
        "id": item_id,
        "bbox": list(bbox),
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [bbox[0], bbox[1]],
                    [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]],
                    [bbox[0], bbox[3]],
                    [bbox[0], bbox[1]],
                ]
            ],
        },
        "properties": {"datetime": dt, "sar:product_type": "GEC"},
        # Sidecar hrefs point at a private bucket -- the walker rewrites them.
        "assets": {"GEC": {"href": "s3://umbra-internal/private/foo_GEC.tif"}},
    }


@pytest.fixture
def fake_bucket(monkeypatch):
    """A tiny in-memory ``sar-data/tasks/`` tree with two acquisitions.

    Layout:
      sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/  -> item "a"
      sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/      -> item "b"
      sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/  -> out-of-range
    """
    listings = {
        # Top level: two task directories.
        "sar-data/tasks/": (
            ["sar-data/tasks/AIR/", "sar-data/tasks/uuid-task/"],
            [],
        ),
        # Named task (one extra UUID level).
        "sar-data/tasks/AIR/": (["sar-data/tasks/AIR/aaaa-uuid/"], []),
        "sar-data/tasks/AIR/aaaa-uuid/": (
            [
                "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/",
                "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/",
            ],
            [],
        ),
        "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/": (
            [],
            [
                "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04.stac.v2.json",
                "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_GEC.tif",
                "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_SICD.nitf",
            ],
        ),
        "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/": (
            [],
            [
                "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/2023-06-01-00-00-00_UMBRA-04.stac.v2.json",
                "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/2023-06-01-00-00-00_UMBRA-04_GEC.tif",
            ],
        ),
        # UUID-style task (sidecar one level shallower).
        "sar-data/tasks/uuid-task/": (
            ["sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/"],
            [],
        ),
        "sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/": (
            [],
            [
                "sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09.stac.v2.json",
                "sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09_GEC.tif",
            ],
        ),
    }
    sidecars = {
        "2024-01-15-10-00-00_UMBRA-04": _sidecar("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
        "2024-02-10-12-00-00_UMBRA-09": _sidecar("b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)),
        "2023-06-01-00-00-00_UMBRA-04": _sidecar("c", "2023-06-01T00:00:00Z", (5, 5, 6, 6)),
    }

    listed: list[str] = []
    fetched: list[str] = []

    def fake_list(self, prefix):
        listed.append(prefix)
        if prefix not in listings:
            raise KeyError(prefix)
        return listings[prefix]

    def fake_get(self, url):
        fetched.append(url)
        for stem, doc in sidecars.items():
            if url.endswith(f"{stem}.stac.v2.json"):
                return doc
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_list_prefix", fake_list)
    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    cat = UmbraCatalog()
    cat._listed = listed
    cat._fetched = fetched
    return cat


def test_search_walks_named_and_uuid_tasks(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    assert sorted(i.id for i in items) == ["a", "b"]


def test_search_prunes_out_of_range_acquisitions(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    # The 2023 acquisition directory must have been pruned -- never listed.
    assert "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/" not in fake_bucket._listed
    # And its sidecar must never have been fetched.
    assert not any("2023-06-01" in u for u in fake_bucket._fetched)
    assert "c" not in {i.id for i in items}


def test_search_assets_have_public_urls(fake_bucket):
    [a] = [i for i in fake_bucket.search(start="2024-01-15", end="2024-01-15")]
    href = a.asset_href("GEC")
    assert href.startswith("https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/")
    assert href.endswith("2024-01-15-10-00-00_UMBRA-04_GEC.tif")
    # The private sidecar URL must not leak through.
    assert "umbra-internal" not in href


def test_search_bbox_filter(fake_bucket):
    items = list(fake_bucket.search(bbox=(0, 0, 5, 5), start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == ["a"]


def test_search_product_type_filter(fake_bucket):
    # Item "b" exposes only GEC; item "a" exposes both GEC and SICD.
    items = list(fake_bucket.search(product_types=["SICD"], start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == ["a"]


def test_search_limit(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31", limit=1))
    assert len(items) == 1
