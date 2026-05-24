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
    """A tiny in-memory ``sar-data/tasks/`` tree with three acquisitions.

    Layout:
      sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/  -> item "a"
      sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/  -> item "c" (out of range)
      sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/      -> item "b"
    """
    # Top-level task discovery uses _list_prefix with delimiter.
    top_subdirs = ["sar-data/tasks/AIR/", "sar-data/tasks/uuid-task/"]

    # Each task is then streamed in full (one paginated LIST per task) via
    # _stream_keys: keys include the sidecar and every data file.
    task_keys = {
        "sar-data/tasks/AIR/": [
            "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04.stac.v2.json",
            "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_GEC.tif",
            "sar-data/tasks/AIR/aaaa-uuid/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_SICD.nitf",
            "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/2023-06-01-00-00-00_UMBRA-04.stac.v2.json",
            "sar-data/tasks/AIR/aaaa-uuid/2023-06-01-00-00-00_UMBRA-04/2023-06-01-00-00-00_UMBRA-04_GEC.tif",
        ],
        "sar-data/tasks/uuid-task/": [
            "sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09.stac.v2.json",
            "sar-data/tasks/uuid-task/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09_GEC.tif",
        ],
    }
    sidecars = {
        "2024-01-15-10-00-00_UMBRA-04": _sidecar("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
        "2024-02-10-12-00-00_UMBRA-09": _sidecar("b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)),
        "2023-06-01-00-00-00_UMBRA-04": _sidecar("c", "2023-06-01T00:00:00Z", (5, 5, 6, 6)),
    }

    listed: list[str] = []
    streamed: list[str] = []
    fetched: list[str] = []

    def fake_list(self, prefix):
        listed.append(prefix)
        if prefix == "sar-data/tasks/":
            return (top_subdirs, [])
        raise KeyError(prefix)

    def fake_stream(self, prefix):
        streamed.append(prefix)
        if prefix not in task_keys:
            raise KeyError(prefix)
        yield from task_keys[prefix]

    def fake_get(self, url):
        fetched.append(url)
        for stem, doc in sidecars.items():
            if url.endswith(f"{stem}.stac.v2.json"):
                return doc
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_list_prefix", fake_list)
    monkeypatch.setattr(UmbraCatalog, "_stream_keys", fake_stream)
    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    cat = UmbraCatalog()
    cat._listed = listed
    cat._streamed = streamed
    cat._fetched = fetched
    return cat


def test_search_walks_named_and_uuid_tasks(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    assert sorted(i.id for i in items) == ["a", "b"]


def test_search_prunes_out_of_range_acquisitions(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    # The 2023 acquisition's sidecar must never have been fetched: keys are
    # date-filtered before the GET.
    assert not any("2023-06-01" in u for u in fake_bucket._fetched)
    assert "c" not in {i.id for i in items}


def test_search_uses_one_stream_per_task(fake_bucket):
    """The walker must issue exactly one streaming LIST per task -- the
    whole point of the v2 rewrite is to avoid per-acquisition LIST calls."""
    list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    assert sorted(fake_bucket._streamed) == [
        "sar-data/tasks/AIR/",
        "sar-data/tasks/uuid-task/",
    ]


def test_search_assets_have_public_urls(fake_bucket):
    [a] = list(fake_bucket.search(start="2024-01-15", end="2024-01-15"))
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


def test_search_url_encodes_spaces_in_task_names(monkeypatch):
    """Named tasks like 'Allegiant Stadium' have spaces in their path;
    asset hrefs must be percent-encoded or rasterio/CURL rejects them."""
    monkeypatch.setattr(
        UmbraCatalog,
        "_list_prefix",
        lambda self, prefix: (["sar-data/tasks/Allegiant Stadium/"], []),
    )
    acq = "sar-data/tasks/Allegiant Stadium/uuid/2024-01-15-10-00-00_UMBRA-04"
    monkeypatch.setattr(
        UmbraCatalog,
        "_stream_keys",
        lambda self, prefix: iter(
            [
                f"{acq}/2024-01-15-10-00-00_UMBRA-04.stac.v2.json",
                f"{acq}/2024-01-15-10-00-00_UMBRA-04_GEC.tif",
            ]
        ),
    )
    monkeypatch.setattr(
        UmbraCatalog,
        "_get",
        lambda self, url: _sidecar("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
    )

    [item] = list(UmbraCatalog().search(start="2024-01-15", end="2024-01-15"))
    href = item.asset_href("GEC")
    assert " " not in href
    assert "Allegiant%20Stadium" in href
