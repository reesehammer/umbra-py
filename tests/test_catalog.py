import threading
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from umbra_py.catalog import _SIDECAR_WORKERS, UmbraCatalog, _acq_date, _task_name
from umbra_py.exceptions import CatalogError


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


def _one_task_catalog(monkeypatch, task_name):
    """A catalog with a single named task holding one 2024 acquisition.

    Returns the catalog; the task is pruned before listing unless ``area``
    matches, so ``cat._streamed`` reveals whether the query matched.
    """
    prefix = f"sar-data/tasks/{task_name}/"
    acq = "2024-01-15-10-00-00_UMBRA-04"
    keys = [f"{prefix}uuid/{acq}/{acq}.stac.v2.json", f"{prefix}uuid/{acq}/{acq}_GEC.tif"]
    monkeypatch.setattr(UmbraCatalog, "_list_prefix", lambda self, p: ([prefix], []))
    streamed: list[str] = []

    def fake_stream(self, p):
        streamed.append(p)
        return iter(keys)

    monkeypatch.setattr(UmbraCatalog, "_stream_keys", fake_stream)
    monkeypatch.setattr(
        UmbraCatalog,
        "_get",
        lambda self, url: _sidecar("hit", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
    )
    cat = UmbraCatalog()
    cat._streamed = streamed
    return cat


def test_search_fuzzy_matches_word_order_and_typos(monkeypatch):
    """fuzzy=True widens area to a word-order-independent, typo-tolerant match."""
    cat = _one_task_catalog(monkeypatch, "Centerfield, Utah")
    for query in ("utah centerfield", "centerfield utah", "centrfield"):
        cat._streamed.clear()
        items = list(cat.search(start="2024-01-01", end="2024-12-31", area=query, fuzzy=True))
        assert {i.id for i in items} == {"hit"}, query


def test_search_fuzzy_off_keeps_substring_only(monkeypatch):
    """Without fuzzy, a reordered query does not match and the task is pruned."""
    cat = _one_task_catalog(monkeypatch, "Centerfield, Utah")
    items = list(cat.search(start="2024-01-01", end="2024-12-31", area="utah centerfield"))
    assert items == []
    # Pruned before listing -- never streamed.
    assert cat._streamed == []


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
    assert captured["fuzzy"] is False


def test_cli_search_fuzzy_flag_flows_through(monkeypatch):
    """`umbra search --area X --fuzzy` reaches catalog.search with fuzzy=True."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([])

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", fake_search)
    result = CliRunner().invoke(cli_mod.cli, ["search", "--area", "utah centerfield", "--fuzzy"])
    assert result.exit_code == 0, result.output
    assert captured["fuzzy"] is True


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


# -- XML hardening (docs/CODEBASE_ANALYSIS.md §6 P2 #13) ---------------------
#
# The bucket listing is remote, untrusted XML parsed on the core discovery
# path. ``UmbraCatalog._parse_listing`` routes it through defusedxml so an
# entity-expansion ("billion laughs") or external-entity (XXE) payload is
# rejected outright rather than exhausting memory or reaching the filesystem.

_BILLION_LAUGHS = (
    '<?xml version="1.0"?>'
    "<!DOCTYPE lolz ["
    '<!ENTITY lol "lol">'
    '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
    "]>"
    f'<ListBucketResult xmlns="{_S3_NS}"><Contents><Key>&lol2;</Key></Contents>'
    "</ListBucketResult>"
).encode()

_XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
    f'<ListBucketResult xmlns="{_S3_NS}"><Contents><Key>&xxe;</Key></Contents>'
    "</ListBucketResult>"
).encode()


@pytest.mark.parametrize("payload", [_BILLION_LAUGHS, _XXE], ids=["billion-laughs", "xxe"])
def test_parse_listing_rejects_malicious_xml(payload):
    """DTD / entity-expansion / external-entity payloads raise, never expand."""
    with pytest.raises(CatalogError):
        UmbraCatalog._parse_listing(payload)


def test_parse_listing_rejects_malformed_xml():
    """Truncated / non-XML bodies fail loudly as a CatalogError, not a raw traceback."""
    with pytest.raises(CatalogError):
        UmbraCatalog._parse_listing(b"<ListBucketResult><Contents>")


def test_parse_listing_accepts_well_formed_listing():
    """A normal ListObjectsV2 body still parses to its keys."""
    body = _list_result(contents=["sar-data/tasks/a/k0"], common_prefixes=["sar-data/tasks/a/"])
    root = UmbraCatalog._parse_listing(body.encode())
    assert root.findtext(f"{{{_S3_NS}}}Contents/{{{_S3_NS}}}Key") == "sar-data/tasks/a/k0"


@responses.activate
def test_list_prefix_rejects_malicious_response_body():
    """An end-to-end hostile listing response is refused, not parsed."""
    cat = UmbraCatalog()
    responses.add(responses.GET, f"{cat._list_base}/", body=_BILLION_LAUGHS, status=200)
    with pytest.raises(CatalogError):
        cat._list_prefix("sar-data/tasks/")


# -- concurrent sidecar fetching (docs/CODEBASE_ANALYSIS.md §4.2 / #9) --------


def _single_task_catalog(monkeypatch, stems, fake_get):
    """A catalog with one task holding an acquisition per stem in ``stems``.

    ``fake_get`` is installed as ``UmbraCatalog._get`` so a test controls the
    per-sidecar behaviour (timing, concurrency accounting, ...).
    """
    task = "sar-data/tasks/Big Site/"
    keys: list[str] = []
    for s in stems:
        keys += [f"{task}{s}/{s}.stac.v2.json", f"{task}{s}/{s}_GEC.tif"]
    monkeypatch.setattr(UmbraCatalog, "_list_prefix", lambda self, prefix: ([task], []))
    monkeypatch.setattr(
        UmbraCatalog,
        "_stream_keys",
        lambda self, prefix: iter(keys if prefix == task else []),
    )
    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    return UmbraCatalog()


def _stem_sidecar(url):
    stem = url.rsplit("/", 1)[-1].removesuffix(".stac.v2.json")
    return _sidecar(stem, f"{stem[:10]}T10:00:00Z", (0, 0, 1, 1))


def test_search_yields_sidecars_in_date_order_despite_concurrency(monkeypatch):
    """Sidecars are fetched concurrently but yielded in acquisition-date order.

    Earlier acquisitions are made to take *longer*, so if the walk yielded in
    completion order the output would be reversed; only an order-preserving
    merge yields them ascending. Guards the determinism §4.2 calls out.
    """
    stems = [f"2024-{m:02d}-01-10-00-00_UMBRA-04" for m in range(1, 7)]

    def fake_get(self, url):
        month = int(url.rsplit("/", 1)[-1][5:7])
        time.sleep((7 - month) * 0.02)  # earlier month -> longer fetch
        return _stem_sidecar(url)

    cat = _single_task_catalog(monkeypatch, stems, fake_get)
    items = list(cat.search(start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == stems


def test_search_fetches_sidecars_concurrently(monkeypatch):
    """More than one sidecar GET is in flight at once -- not a serial N+1."""
    stems = [f"2024-{m:02d}-01-10-00-00_UMBRA-04" for m in range(1, 9)]
    lock = threading.Lock()
    state = {"in_flight": 0, "peak": 0}

    def fake_get(self, url):
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        try:
            time.sleep(0.05)
        finally:
            with lock:
                state["in_flight"] -= 1
        return _stem_sidecar(url)

    cat = _single_task_catalog(monkeypatch, stems, fake_get)
    items = list(cat.search(start="2024-01-01", end="2024-12-31"))
    assert len(items) == len(stems)
    assert state["peak"] > 1  # genuinely parallel, not one-at-a-time


def test_search_limit_bounds_sidecar_overfetch(monkeypatch):
    """A tiny ``limit`` must not drag in every sidecar of a large task.

    Fetching in windows caps wasted work at one worker window even though the
    task holds far more in-range acquisitions than the limit asks for.
    """
    stems = [f"2024-01-{d:02d}-10-00-00_UMBRA-04" for d in range(1, 21)]
    fetched: list[str] = []
    lock = threading.Lock()

    def fake_get(self, url):
        with lock:
            fetched.append(url)
        return _stem_sidecar(url)

    cat = _single_task_catalog(monkeypatch, stems, fake_get)
    items = list(cat.search(start="2024-01-01", end="2024-12-31", limit=1))
    assert [i.id for i in items] == [stems[0]]
    assert len(fetched) <= _SIDECAR_WORKERS


def test_search_single_acquisition_task_still_yields(monkeypatch):
    """The one-sidecar fast path (no thread pool) still returns the item."""
    stems = ["2024-05-01-10-00-00_UMBRA-04"]
    cat = _single_task_catalog(monkeypatch, stems, lambda self, url: _stem_sidecar(url))
    items = list(cat.search(start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == stems
