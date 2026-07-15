"""Tests for the Canopy commercial-archive backend of :class:`UmbraCatalog`.

The open-data path crawls a static S3 bucket; the commercial path (enabled by
passing a ``token``) queries Umbra's authenticated STAC API instead. These tests
mock that API with ``responses`` -- no credentials or network needed -- and
assert the two paths share one ``search()`` interface and yield the same
:class:`UmbraItem` objects.
"""

from __future__ import annotations

import json

import pytest
import responses

from umbra_py.catalog import UmbraCatalog, _datetime_interval, _next_link
from umbra_py.constants import CANOPY_ARCHIVE_URL
from umbra_py.exceptions import CatalogError

# Canopy returns standard STAC features whose asset hrefs are already resolvable
# URLs (unlike the open bucket's private-bucket sidecars we have to rewrite).
_GEOTIFF = "image/tiff; application=geotiff; profile=cloud-optimized"


def _feature(item_id: str, dt: str, bbox: tuple, *, task: str = "Test Site") -> dict:
    return {
        "type": "Feature",
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
        "properties": {
            "datetime": dt,
            "sar:product_type": "GEC",
            "umbra:task_id": task,
        },
        "assets": {
            "GEC": {
                "href": f"https://api.canopy.umbra.space/data/{item_id}_GEC.tif",
                "type": _GEOTIFF,
            }
        },
    }


def _collection(*features: dict, links: list | None = None) -> dict:
    return {
        "type": "FeatureCollection",
        "features": list(features),
        "links": links or [],
    }


# -- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (None, None, None),
        ("2024-01-01", "2024-01-31", "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"),
        ("2024-06-01", None, "2024-06-01T00:00:00Z/.."),
        (None, "2024-06-30", "../2024-06-30T23:59:59Z"),
    ],
)
def test_datetime_interval(start, end, expected):
    from umbra_py.catalog import _coerce_date

    lo = _coerce_date(start)
    hi = _coerce_date(end, is_end=True)
    assert _datetime_interval(lo, hi) == expected


def test_next_link_picks_rel_next():
    links = [
        {"rel": "self", "href": "a"},
        {"rel": "next", "href": "b", "method": "POST"},
    ]
    assert _next_link(links) == {"rel": "next", "href": "b", "method": "POST"}
    assert _next_link([{"rel": "self", "href": "a"}]) is None
    assert _next_link([{"rel": "next"}]) is None  # href missing -> ignored


# -- search dispatch ----------------------------------------------------------


def test_token_selects_archive_backend():
    assert UmbraCatalog().token is None
    assert UmbraCatalog(token="secret").token == "secret"


@responses.activate
def test_search_archive_yields_items():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
            _feature("b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)),
        ),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == ["a", "b"]
    # Asset hrefs from the real STAC API pass through unchanged.
    assert items[0].asset_href("GEC").endswith("a_GEC.tif")


@responses.activate
def test_search_archive_sends_bearer_auth_and_filters():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(_feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1))),
        status=200,
    )
    cat = UmbraCatalog(token="secret", collections=["umbra-archive"])
    list(cat.search(bbox=(0, 0, 2, 2), start="2024-01-01", end="2024-01-31"))

    assert len(responses.calls) == 1
    req = responses.calls[0].request
    assert req.headers["Authorization"] == "Bearer secret"
    body = json.loads(req.body)
    assert body["bbox"] == [0, 0, 2, 2]
    assert body["datetime"] == "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"
    assert body["collections"] == ["umbra-archive"]


@responses.activate
def test_search_archive_paginates_via_next_link():
    """A ``rel=next`` POST link (with merge body) drives a second page."""
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
            links=[
                {
                    "rel": "next",
                    "href": CANOPY_ARCHIVE_URL,
                    "method": "POST",
                    "merge": True,
                    "body": {"token": "page2"},
                }
            ],
        ),
        status=200,
    )
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(_feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3))),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(start="2024-01-01", end="2024-12-31"))
    assert [i.id for i in items] == ["a", "b"]
    # The merged next body carries the original filters plus the page token.
    page2 = json.loads(responses.calls[1].request.body)
    assert page2["token"] == "page2"
    assert page2["datetime"] == "2024-01-01T00:00:00Z/2024-12-31T23:59:59Z"


@responses.activate
def test_search_archive_paginates_via_get_next_link():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
            links=[{"rel": "next", "href": CANOPY_ARCHIVE_URL + "?token=page2"}],
        ),
        status=200,
    )
    responses.add(
        responses.GET,
        CANOPY_ARCHIVE_URL,
        json=_collection(_feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3))),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search())
    assert [i.id for i in items] == ["a", "b"]
    assert responses.calls[1].request.method == "GET"


@responses.activate
def test_search_archive_limit_stops_early():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
            _feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3)),
            _feature("c", "2024-03-10T12:00:00Z", (4, 4, 5, 5)),
        ),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(limit=2))
    assert [i.id for i in items] == ["a", "b"]


@responses.activate
def test_search_archive_product_type_filter_client_side():
    # Only "a" gets a SICD asset; the product filter is applied to returned items.
    feat_a = _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1))
    feat_a["assets"]["SICD"] = {
        "href": "https://api.canopy.umbra.space/data/a_SICD.nitf",
        "type": "application/vnd.nitf",
    }
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(feat_a, _feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3))),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(product_types=["SICD"]))
    assert [i.id for i in items] == ["a"]


@responses.activate
def test_search_archive_area_filter_client_side():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1), task="Port of Long Beach"),
            _feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3), task="Centerfield, Utah"),
        ),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(area="long beach"))
    assert [i.id for i in items] == ["a"]
    # Fuzzy widens it (word order / typo tolerant) without dropping the match.
    items = list(UmbraCatalog(token="secret").search(area="beach long", fuzzy=True))
    assert [i.id for i in items] == ["a"]


@responses.activate
def test_search_archive_max_per_task():
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json=_collection(
            _feature("a1", "2024-01-15T10:00:00Z", (0, 0, 1, 1), task="Site A"),
            _feature("a2", "2024-01-16T10:00:00Z", (0, 0, 1, 1), task="Site A"),
            _feature("b1", "2024-02-10T12:00:00Z", (2, 2, 3, 3), task="Site B"),
        ),
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    items = list(cat.search(max_per_task=1))
    assert [i.id for i in items] == ["a1", "b1"]


@responses.activate
def test_search_archive_auth_error_is_helpful():
    responses.add(responses.POST, CANOPY_ARCHIVE_URL, status=401)
    cat = UmbraCatalog(token="bad")
    with pytest.raises(CatalogError, match="rejected the token"):
        list(cat.search())


@responses.activate
def test_search_archive_server_error_wrapped():
    responses.add(responses.POST, CANOPY_ARCHIVE_URL, status=500)
    cat = UmbraCatalog(token="secret")
    with pytest.raises(CatalogError, match="Canopy archive search failed"):
        list(cat.search())
