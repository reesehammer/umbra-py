"""Tests for the umbra STAC API façade (``umbra_py.serve``).

The whole module is skipped when the ``serve`` extra (FastAPI) is not
installed, so the core CI job never sees it; the all-extras job installs
``[dev,all,mcp,serve]`` and runs it. Everything here is offline: the API is
driven with FastAPI's in-process ``TestClient`` against a temporary
:class:`~umbra_py.CatalogIndex`, so no live catalog access is required and the
suite stays deterministic. The pure STAC document builders are also tested
directly, without a running server.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from umbra_py import serve  # noqa: E402
from umbra_py.index import CatalogIndex  # noqa: E402
from umbra_py.models import UmbraItem  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
COLLECTION = serve.COLLECTION_ID


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())


def _href(idx: int) -> str:
    return (
        "https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/tasks/"
        f"Testville/2024-01-0{idx + 1}-00-00-00_UMBRA-0{idx + 1}/x{idx}.stac.v2.json"
    )


@pytest.fixture
def index_path(tmp_path, sample_item_dict) -> Path:
    """A temp index holding three dated copies of the sample item."""
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        for i in range(3):
            doc = copy.deepcopy(sample_item_dict)
            doc["id"] = f"item-{i}"
            idx.add(UmbraItem.from_dict(doc, href=_href(i)))
    return path


@pytest.fixture
def client(index_path) -> TestClient:
    return TestClient(serve.build_app(index_path))


# --------------------------------------------------------------------------
# Pure document builders (no server needed)
# --------------------------------------------------------------------------


def test_landing_page_is_a_conformant_catalog():
    page = serve.landing_page("http://localhost:8000/")
    assert page["type"] == "Catalog"
    assert page["stac_version"] == serve.STAC_VERSION
    assert set(serve.CONFORMANCE_CLASSES) <= set(page["conformsTo"])
    rels = {link["rel"] for link in page["links"]}
    assert {"self", "root", "conformance", "data", "search", "service-desc"} <= rels
    # No trailing double-slash from base_url normalisation.
    assert all("//collections" not in link["href"] for link in page["links"])


def test_collection_carries_license_and_temporal_extent():
    coll = serve.collection("http://localhost:8000", temporal=("2023-01-01", "2024-06-01"))
    assert coll["id"] == COLLECTION
    assert coll["license"] == "CC-BY-4.0"
    assert coll["extent"]["temporal"]["interval"] == [["2023-01-01", "2024-06-01"]]
    assert coll["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]


def test_item_to_stac_stamps_collection_and_rewrites_links(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href=_href(0))
    feature = serve.item_to_stac(item, "http://localhost:8000")
    assert feature["type"] == "Feature"
    assert feature["collection"] == COLLECTION
    self_link = next(link for link in feature["links"] if link["rel"] == "self")
    assert self_link["href"].endswith(f"/collections/{COLLECTION}/items/{item.id}")
    # The original static-catalog relative links are replaced, not appended.
    assert {link["rel"] for link in feature["links"]} == {"self", "root", "parent", "collection"}


def test_parse_bbox_accepts_2d_and_3d():
    assert serve.parse_bbox("1,2,3,4") == (1.0, 2.0, 3.0, 4.0)
    assert serve.parse_bbox("1,2,5,3,4,6") == (1.0, 2.0, 3.0, 4.0)  # z dropped
    assert serve.parse_bbox(None) is None
    with pytest.raises(ValueError):
        serve.parse_bbox("1,2,3")


def test_parse_datetime_handles_instants_and_intervals():
    from datetime import date

    assert serve.parse_datetime(None) == (None, None)
    assert serve.parse_datetime("2024-01-01") == (date(2024, 1, 1), date(2024, 1, 1))
    assert serve.parse_datetime("2024-01-01/2024-02-01") == (date(2024, 1, 1), date(2024, 2, 1))
    assert serve.parse_datetime("2024-01-01/..") == (date(2024, 1, 1), None)
    assert serve.parse_datetime("../2024-02-01") == (None, date(2024, 2, 1))
    # RFC3339 datetimes are accepted (the index prunes on date).
    assert serve.parse_datetime("2024-01-01T12:00:00Z")[0] == date(2024, 1, 1)


# --------------------------------------------------------------------------
# API endpoints (in-process TestClient)
# --------------------------------------------------------------------------


def test_landing_and_conformance_endpoints(client):
    assert client.get("/").json()["type"] == "Catalog"
    body = client.get("/conformance").json()
    assert set(serve.CONFORMANCE_CLASSES) <= set(body["conformsTo"])


def test_collections_endpoint_lists_the_single_collection(client):
    body = client.get("/collections").json()
    ids = [c["id"] for c in body["collections"]]
    assert ids == [COLLECTION]
    assert client.get(f"/collections/{COLLECTION}").json()["id"] == COLLECTION
    assert client.get("/collections/does-not-exist").status_code == 404


def test_items_endpoint_returns_geojson_featurecollection(client):
    resp = client.get(f"/collections/{COLLECTION}/items")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 3
    assert all(f["collection"] == COLLECTION for f in body["features"])


def test_single_item_by_id(client):
    resp = client.get(f"/collections/{COLLECTION}/items/item-1")
    assert resp.status_code == 200
    assert resp.json()["id"] == "item-1"
    assert client.get(f"/collections/{COLLECTION}/items/nope").status_code == 404


def test_search_filters_by_bbox(client):
    hit = client.get("/search?bbox=-69,10,-67,11").json()
    assert len(hit["features"]) == 3
    miss = client.get("/search?bbox=0,0,1,1").json()
    assert miss["features"] == []


def test_search_filters_by_datetime(client):
    hit = client.get("/search?datetime=2024-01-01/2024-01-31").json()
    assert len(hit["features"]) == 3
    miss = client.get("/search?datetime=2025-01-01/2025-12-31").json()
    assert miss["features"] == []


def test_search_filters_by_ids(client):
    body = client.get("/search?ids=item-0,item-2").json()
    assert {f["id"] for f in body["features"]} == {"item-0", "item-2"}


def test_search_paginates_with_next_link(client):
    page1 = client.get("/search?limit=2").json()
    assert len(page1["features"]) == 2
    next_links = [link for link in page1["links"] if link["rel"] == "next"]
    assert next_links, "expected a next link when more results remain"
    # Follow it (strip the test host prefix to a relative path).
    href = next_links[0]["href"].replace("http://testserver", "")
    page2 = client.get(href).json()
    assert len(page2["features"]) == 1
    assert not [link for link in page2["links"] if link["rel"] == "next"]


def test_post_search_matches_get_search(client):
    resp = client.post("/search", json={"bbox": [-69, 10, -67, 11], "limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()["features"]) == 3


def test_search_rejects_unknown_collection(client):
    resp = client.post("/search", json={"collections": ["sentinel-1"]})
    assert resp.status_code == 400


def test_bad_bbox_is_a_client_error(client):
    assert client.get("/search?bbox=1,2,3").status_code == 400


def test_openapi_document_is_served(client):
    assert client.get("/openapi.json").status_code == 200


def test_missing_index_reports_service_unavailable(tmp_path):
    app = serve.build_app(tmp_path / "absent.db")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/search")
    assert resp.status_code == 503
    assert "umbra index" in resp.json()["detail"]


def test_run_search_offset_paging_is_stable(index_path):
    with CatalogIndex(index_path) as source:
        first, has_next = serve.run_search(source, limit=2, offset=0)
        assert has_next is True
        assert [i.id for i in first] == ["item-0", "item-1"]
    with CatalogIndex(index_path) as source:
        second, has_next = serve.run_search(source, limit=2, offset=2)
        assert has_next is False
        assert [i.id for i in second] == ["item-2"]
