from datetime import date

import pytest

from umbra_py.catalog import (
    UmbraCatalog,
    _spans_overlap,
    _token_from_href,
    _token_span,
)


@pytest.mark.parametrize(
    "token,expected",
    [
        ("2024", (date(2024, 1, 1), date(2024, 12, 31))),
        ("2024-02", (date(2024, 2, 1), date(2024, 2, 29))),  # leap year
        ("2023-02", (date(2023, 2, 1), date(2023, 2, 28))),
        ("2024-01-15", (date(2024, 1, 15), date(2024, 1, 15))),
        ("not-a-date", None),
    ],
)
def test_token_span(token, expected):
    assert _token_span(token) == expected


def test_spans_overlap():
    span = (date(2024, 1, 1), date(2024, 1, 31))
    assert _spans_overlap(span, date(2024, 1, 15), date(2024, 2, 1))
    assert _spans_overlap(span, None, None)
    assert not _spans_overlap(span, date(2024, 2, 1), None)
    assert not _spans_overlap(span, None, date(2023, 12, 31))


def test_token_from_href():
    assert _token_from_href("./2024-01/catalog.json") == "2024-01"
    assert _token_from_href("./2024/catalog.json") == "2024"
    assert _token_from_href("./2024-01-01/catalog.json") == "2024-01-01"


def _make_tree():
    """A tiny in-memory catalog tree keyed by URL."""
    item_a = {
        "id": "a",
        "bbox": [0, 0, 1, 1],
        "properties": {"datetime": "2024-01-01T00:00:00Z", "sar:product_type": "GEC"},
        "assets": {"GEC": {"href": "http://x/a_GEC.tif"}},
    }
    item_b = {
        "id": "b",
        "bbox": [10, 10, 11, 11],
        "properties": {"datetime": "2024-02-01T00:00:00Z", "sar:product_type": "SICD"},
        "assets": {"SICD": {"href": "http://x/b_SICD.nitf"}},
    }
    return {
        "root": {
            "links": [
                {"rel": "child", "href": "./2024/catalog.json", "title": "2024"},
                {"rel": "child", "href": "./2023/catalog.json", "title": "2023"},
            ]
        },
        "./2024/catalog.json": {
            "links": [
                {"rel": "child", "href": "./2024-01/catalog.json", "title": "2024-01"},
                {"rel": "child", "href": "./2024-02/catalog.json", "title": "2024-02"},
            ]
        },
        "./2024-01/catalog.json": {"links": [{"rel": "item", "href": "./a.json"}]},
        "./2024-02/catalog.json": {"links": [{"rel": "item", "href": "./b.json"}]},
        "./2023/catalog.json": {"links": []},  # should never be fetched given date filter
        "./a.json": item_a,
        "./b.json": item_b,
    }


@pytest.fixture
def fake_catalog(monkeypatch):
    tree = _make_tree()
    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        # Normalise absolute-ish urljoin output back to our tree keys.
        for key in tree:
            if url == key or url.endswith(key.lstrip("./")):
                return tree[key]
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    cat = UmbraCatalog(root_url="root")
    cat._fetched = fetched
    return cat


def test_search_date_filter_prunes_year(fake_catalog):
    items = list(fake_catalog.search(start="2024-01-01", end="2024-01-31"))
    assert [i.id for i in items] == ["a"]
    # The 2023 branch must have been pruned (never fetched).
    assert not any("2023" in u for u in fake_catalog._fetched)


def test_search_product_type_filter(fake_catalog):
    items = list(fake_catalog.search(product_types=["SICD"]))
    assert [i.id for i in items] == ["b"]


def test_search_bbox_filter(fake_catalog):
    items = list(fake_catalog.search(bbox=(0, 0, 5, 5)))
    assert [i.id for i in items] == ["a"]


def test_search_limit(fake_catalog):
    items = list(fake_catalog.search(limit=1))
    assert len(items) == 1
