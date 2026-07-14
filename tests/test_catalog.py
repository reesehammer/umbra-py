from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from umbra_py.catalog import UmbraCatalog, _acq_date, _task_name


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


def test_search_max_per_task_caps_revisits(monkeypatch):
    """max_per_task yields at most N items per top-level task -- one per
    distinct site rather than every revisit, for map diversity."""
    monkeypatch.setattr(
        UmbraCatalog,
        "_list_prefix",
        lambda self, prefix: (["sar-data/tasks/site-a/", "sar-data/tasks/site-b/"], []),
    )
    # Two revisits at site-a, one acquisition at site-b.
    task_keys = {
        "sar-data/tasks/site-a/": [
            "sar-data/tasks/site-a/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04.stac.v2.json",
            "sar-data/tasks/site-a/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_GEC.tif",
            "sar-data/tasks/site-a/2024-02-20-10-00-00_UMBRA-04/2024-02-20-10-00-00_UMBRA-04.stac.v2.json",
            "sar-data/tasks/site-a/2024-02-20-10-00-00_UMBRA-04/2024-02-20-10-00-00_UMBRA-04_GEC.tif",
        ],
        "sar-data/tasks/site-b/": [
            "sar-data/tasks/site-b/2024-03-10-10-00-00_UMBRA-09/2024-03-10-10-00-00_UMBRA-09.stac.v2.json",
            "sar-data/tasks/site-b/2024-03-10-10-00-00_UMBRA-09/2024-03-10-10-00-00_UMBRA-09_GEC.tif",
        ],
    }
    monkeypatch.setattr(UmbraCatalog, "_stream_keys", lambda self, prefix: iter(task_keys[prefix]))
    sidecars = {
        "2024-01-15-10-00-00_UMBRA-04": _sidecar("a1", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
        "2024-02-20-10-00-00_UMBRA-04": _sidecar("a2", "2024-02-20T10:00:00Z", (0, 0, 1, 1)),
        "2024-03-10-10-00-00_UMBRA-09": _sidecar("b1", "2024-03-10T10:00:00Z", (10, 10, 11, 11)),
    }

    def fake_get(self, url):
        for stem, doc in sidecars.items():
            if url.endswith(f"{stem}.stac.v2.json"):
                return doc
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)

    cat = UmbraCatalog()
    # Without the cap, both site-a revisits are returned.
    assert sorted(i.id for i in cat.search(start="2024-01-01", end="2024-12-31")) == [
        "a1",
        "a2",
        "b1",
    ]
    # With max_per_task=1, exactly one item per task.
    items = list(cat.search(start="2024-01-01", end="2024-12-31", max_per_task=1))
    assert len(items) == 2
    assert {i.id for i in items} == {"a1", "b1"}


def test_task_name_strips_prefix_and_slash():
    assert _task_name("sar-data/tasks/Centerfield, Utah/") == "Centerfield, Utah"


def test_search_area_filters_to_matching_task(fake_bucket):
    """area= keeps only task directories whose name contains the substring."""
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31", area="AIR"))
    assert {i.id for i in items} == {"a"}
    # The non-matching task is pruned *before* listing -- never streamed.
    assert fake_bucket._streamed == ["sar-data/tasks/AIR/"]


def test_search_area_is_case_insensitive(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31", area="air"))
    assert {i.id for i in items} == {"a"}


def test_search_area_no_match_yields_nothing(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31", area="nowhere"))
    assert items == []
    # Nothing matched, so no task was listed at all.
    assert fake_bucket._streamed == []


def test_cli_search_area_flows_through(monkeypatch):
    """`umbra search --area X` reaches catalog.search with area=X."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([])

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", fake_search)
    result = CliRunner().invoke(cli_mod.cli, ["search", "--area", "Centerfield"])
    assert result.exit_code == 0, result.output
    assert captured["area"] == "Centerfield"


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


# -- S3 pagination protocol regression --------------------------------------
#
# Umbra's bucket is listed with the anonymous S3 REST API. The lister must send
# ``list-type=2`` (ListObjectsV2); without it S3 serves the V1 API, which
# ignores ``continuation-token`` and never returns ``NextContinuationToken`` --
# so any task with more than 1,000 keys was silently truncated to its first
# page. These tests drive the real ``_list_prefix`` / ``_stream_keys`` (not the
# monkeypatched fakes the other tests use) against a fake two-page bucket.

_S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
_NEXT_TOKEN = "PAGE2TOKEN"


def _list_result(*, contents=(), common_prefixes=(), next_token=None):
    """Build a minimal ListObjectsV2 XML response body."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<ListBucketResult xmlns="{_S3_NS}">']
    for p in common_prefixes:
        parts.append(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>")
    for k in contents:
        parts.append(f"<Contents><Key>{k}</Key></Contents>")
    if next_token is not None:
        parts.append("<IsTruncated>true</IsTruncated>")
        parts.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
    else:
        parts.append("<IsTruncated>false</IsTruncated>")
    parts.append("</ListBucketResult>")
    return "".join(parts)


def _make_paged_callback(page1, page2, seen_urls):
    """Return a ``responses`` callback that serves page1 then page2.

    Which page is served is decided by the presence of ``continuation-token``
    in the request (exactly how a real V2 client paginates). Every request URL
    is recorded in ``seen_urls`` so tests can assert ``list-type=2`` was sent.
    """

    def _callback(request):
        qs = parse_qs(urlparse(request.url).query)
        seen_urls.append(request.url)
        body = page2 if "continuation-token" in qs else page1
        return (200, {}, body)

    return _callback


@responses.activate
def test_stream_keys_follows_continuation_token():
    """A truncated task listing is fully consumed across both pages."""
    cat = UmbraCatalog()
    prefix = "sar-data/tasks/Big Task/"
    page1_keys = [f"{prefix}k{i}" for i in range(1000)]
    page2_keys = [f"{prefix}k1000", f"{prefix}k1001"]
    seen_urls = []
    responses.add_callback(
        responses.GET,
        f"{cat._list_base}/",
        callback=_make_paged_callback(
            _list_result(contents=page1_keys, next_token=_NEXT_TOKEN),
            _list_result(contents=page2_keys),
            seen_urls,
        ),
    )

    keys = list(cat._stream_keys(prefix))

    assert len(keys) == 1002  # both pages, not truncated at 1000
    assert keys[-1] == f"{prefix}k1001"
    assert len(seen_urls) == 2
    # Both requests must be V2 and the second must carry the continuation token.
    assert all("list-type=2" in url for url in seen_urls)
    assert "continuation-token=" in seen_urls[1]


@responses.activate
def test_list_prefix_follows_continuation_token():
    """A truncated delimited listing returns every subdir and file, paged."""
    cat = UmbraCatalog()
    prefix = "sar-data/tasks/"
    page1_prefixes = [f"{prefix}task{i}/" for i in range(1000)]
    page2_prefixes = [f"{prefix}task1000/"]
    seen_urls = []
    responses.add_callback(
        responses.GET,
        f"{cat._list_base}/",
        callback=_make_paged_callback(
            _list_result(common_prefixes=page1_prefixes, next_token=_NEXT_TOKEN),
            _list_result(common_prefixes=page2_prefixes),
            seen_urls,
        ),
    )

    subdirs, _files = cat._list_prefix(prefix)

    assert len(subdirs) == 1001  # both pages
    assert f"{prefix}task1000/" in subdirs
    assert len(seen_urls) == 2
    assert all("list-type=2" in url for url in seen_urls)
    # The delimiter must survive alongside list-type=2 on the first request.
    assert "delimiter=" in seen_urls[0]
