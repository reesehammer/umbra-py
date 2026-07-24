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


def test_item_to_stac_surfaces_baked_place(sample_item_dict):
    """A baked `.place` (from `umbra index bake`) is surfaced as the namespaced
    `umbra:place` property, so a STAC client shows a real place name."""
    item = UmbraItem.from_dict(sample_item_dict, href=_href(0))
    # Absent by default (a live-walk item carries no baked label).
    assert "umbra:place" not in serve.item_to_stac(item, "http://x")["properties"]
    item.place = "Reykjavík, Iceland"
    feature = serve.item_to_stac(item, "http://x")
    assert feature["properties"]["umbra:place"] == "Reykjavík, Iceland"


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


def test_parse_product_types_normalises_and_validates():
    assert serve.parse_product_types(None) is None
    assert serve.parse_product_types("") is None
    assert serve.parse_product_types("gec, sicd") == ["GEC", "SICD"]
    assert serve.parse_product_types(["Cphd"]) == ["CPHD"]
    with pytest.raises(ValueError):
        serve.parse_product_types("GEC,NOPE")


def test_parse_polarizations_normalises():
    assert serve.parse_polarizations(None) is None
    assert serve.parse_polarizations("") is None
    assert serve.parse_polarizations("vv, vh") == ["VV", "VH"]
    assert serve.parse_polarizations(["Hh"]) == ["HH"]
    with pytest.raises(ValueError):
        serve.parse_polarizations(5)


def test_parse_query_maps_extension_to_index_filters():
    assert serve.parse_query(None) == serve.QueryFilters()
    assert serve.parse_query({}) == serve.QueryFilters()
    # Both the operator form and the bare-value shorthand are accepted.
    assert serve.parse_query({"product_types": {"in": ["gec"]}}).product_types == ["GEC"]
    assert serve.parse_query({"product_types": "sicd"}).product_types == ["SICD"]
    assert serve.parse_query({"area": {"like": "Beet"}}).area == "Beet"
    assert serve.parse_query({"area": "Beet"}).area == "Beet"
    # An unknown property or operator is a hard error, never a silent drop.
    with pytest.raises(ValueError):
        serve.parse_query({"datetime": {"gte": "2024"}})
    with pytest.raises(ValueError):
        serve.parse_query({"area": {"gt": "Beet"}})
    with pytest.raises(ValueError):
        serve.parse_query("not-an-object")


def test_parse_query_maps_sar_acquisition_properties():
    # Polarizations: operator form, bare list/string, all upper-cased.
    assert serve.parse_query({"sar:polarizations": {"in": ["vv", "vh"]}}).polarizations == [
        "VV",
        "VH",
    ]
    assert serve.parse_query({"sar:polarizations": "hh"}).polarizations == ["HH"]
    # Incidence: a gte/lte range, either or both bounds.
    q = serve.parse_query({"view:incidence_angle": {"gte": 20, "lte": 40}})
    assert (q.min_incidence, q.max_incidence) == (20.0, 40.0)
    q = serve.parse_query({"view:incidence_angle": {"lte": 40}})
    assert (q.min_incidence, q.max_incidence) == (None, 40.0)
    # Resolution: an lte bound, or a bare-number shorthand for it.
    assert serve.parse_query({"sar:resolution": {"lte": 0.5}}).max_resolution == 0.5
    assert serve.parse_query({"sar:resolution": 0.5}).max_resolution == 0.5
    # Several filters compose in one query object.
    q = serve.parse_query(
        {
            "product_types": {"in": ["GEC"]},
            "sar:polarizations": {"in": ["VV"]},
            "view:incidence_angle": {"gte": 30},
            "sar:resolution": {"lte": 1.0},
        }
    )
    assert q.product_types == ["GEC"]
    assert q.polarizations == ["VV"]
    assert q.min_incidence == 30.0
    assert q.max_resolution == 1.0


def test_parse_query_rejects_bad_sar_operators_and_values():
    # A range property needs an object with gte/lte, not a bare value or eq.
    with pytest.raises(ValueError):
        serve.parse_query({"view:incidence_angle": 30})
    with pytest.raises(ValueError):
        serve.parse_query({"view:incidence_angle": {"eq": 30}})
    with pytest.raises(ValueError):
        serve.parse_query({"view:incidence_angle": {}})
    # Resolution only supports lte; a non-numeric value is a hard error.
    with pytest.raises(ValueError):
        serve.parse_query({"sar:resolution": {"gte": 0.5}})
    with pytest.raises(ValueError):
        serve.parse_query({"view:incidence_angle": {"gte": "wide"}})
    with pytest.raises(ValueError):
        serve.parse_query({"sar:resolution": "fine"})


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


def test_get_one_uses_keyed_lookup_for_an_index(index_path):
    # A CatalogIndex source resolves the item through its keyed get(), not a scan.
    with CatalogIndex(index_path) as idx:
        item = serve.get_one(idx, "item-2")
        assert item is not None and item.id == "item-2"
        assert serve.get_one(idx, "missing") is None


def test_get_one_falls_back_to_search_for_a_listing_source(sample_item_dict):
    # A source that only lists (like the live UmbraCatalog) is filtered by id.
    class _ListingSource:
        def __init__(self, items):
            self._items = items

        def search(self, **kwargs):
            limit = kwargs.get("limit")
            out = list(self._items)
            return out[:limit] if limit is not None else out

    items = []
    for i in range(3):
        doc = copy.deepcopy(sample_item_dict)
        doc["id"] = f"item-{i}"
        items.append(UmbraItem.from_dict(doc, href=_href(i)))
    source = _ListingSource(items)
    assert serve.get_one(source, "item-2").id == "item-2"
    assert serve.get_one(source, "missing") is None


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


def test_conformance_advertises_query_extension(client):
    conforms = client.get("/conformance").json()["conformsTo"]
    assert "https://api.stacspec.org/v1.0.0/item-search#query" in conforms


def test_search_filters_by_product_types(client):
    # The sample items carry GEC/SICD/SIDD/CPHD assets but no CSI.
    assert client.get("/search?product_types=GEC").json()["context"]["returned"] == 3
    assert client.get("/search?product_types=CSI").json()["context"]["returned"] == 0


def test_search_rejects_unknown_product_type(client):
    resp = client.get("/search?product_types=BOGUS")
    assert resp.status_code == 400
    assert "BOGUS" in resp.json()["detail"]


def test_search_filters_by_area(client):
    # The fixture files every item under the "Testville" task.
    assert client.get("/search?area=Testville").json()["context"]["returned"] == 3
    assert client.get("/search?area=Nowhere").json()["context"]["returned"] == 0
    # Fuzzy widens a lowercased token to the same task.
    assert client.get("/search?area=testville&fuzzy=true").json()["context"]["returned"] == 3


def test_items_endpoint_filters_by_product_types(client):
    path = f"/collections/{COLLECTION}/items?product_types=GEC"
    assert client.get(path).json()["context"]["returned"] == 3
    path = f"/collections/{COLLECTION}/items?product_types=CSI"
    assert client.get(path).json()["context"]["returned"] == 0


def test_search_query_params_survive_pagination(client):
    page1 = client.get("/search?product_types=GEC&limit=2").json()
    next_link = next(link for link in page1["links"] if link["rel"] == "next")
    assert "product_types=GEC" in next_link["href"]
    href = next_link["href"].replace("http://testserver", "")
    page2 = client.get(href).json()
    assert page2["context"]["returned"] == 1


def test_post_search_query_extension(client):
    # Operator form, bare-list form and top-level fields all reach the index.
    assert (
        client.post("/search", json={"query": {"product_types": {"in": ["GEC"]}}}).json()[
            "context"
        ]["returned"]
        == 3
    )
    assert (
        client.post("/search", json={"query": {"product_types": ["CSI"]}}).json()["context"][
            "returned"
        ]
        == 0
    )
    assert (
        client.post("/search", json={"query": {"area": {"like": "Testville"}}}).json()["context"][
            "returned"
        ]
        == 3
    )
    assert (
        client.post("/search", json={"product_types": ["GEC"], "area": "Testville"}).json()[
            "context"
        ]["returned"]
        == 3
    )


def test_post_search_rejects_bad_query(client):
    assert client.post("/search", json={"query": {"nope": "x"}}).status_code == 400
    assert client.post("/search", json={"query": {"area": {"gt": "x"}}}).status_code == 400
    assert client.post("/search", json={"product_types": ["BOGUS"]}).status_code == 400


# --------------------------------------------------------------------------
# SAR acquisition-property filters over the API (pol / incidence / resolution)
# --------------------------------------------------------------------------


@pytest.fixture
def sar_client(tmp_path, sample_item_dict) -> TestClient:
    """A client over three items varied by polarization / incidence / resolution.

    item-0: VV,       20 deg, 0.3 m
    item-1: VH,       35 deg, 0.5 m
    item-2: HH, HV,   50 deg, 1.5 m
    """
    specs = [
        (["VV"], 20.0, 0.3),
        (["VH"], 35.0, 0.5),
        (["HH", "HV"], 50.0, 1.5),
    ]
    path = tmp_path / "sar.db"
    with CatalogIndex(path) as idx:
        for i, (pols, inc, res) in enumerate(specs):
            doc = copy.deepcopy(sample_item_dict)
            doc["id"] = f"item-{i}"
            doc["properties"]["sar:polarizations"] = pols
            doc["properties"]["view:incidence_angle"] = inc
            doc["properties"]["sar:resolution_range"] = res
            doc["properties"]["sar:resolution_azimuth"] = res
            idx.add(UmbraItem.from_dict(doc, href=_href(i)))
    return TestClient(serve.build_app(path))


def _returned_ids(body: dict) -> list[str]:
    return [f["id"] for f in body["features"]]


def test_search_filters_by_polarization(sar_client):
    assert _returned_ids(sar_client.get("/search?polarizations=VV").json()) == ["item-0"]
    assert _returned_ids(sar_client.get("/search?polarizations=VH").json()) == ["item-1"]
    # An item is kept if it exposes at least one requested polarization.
    assert _returned_ids(sar_client.get("/search?polarizations=HV").json()) == ["item-2"]
    assert sar_client.get("/search?polarizations=VV,VH").json()["context"]["returned"] == 2


def test_search_filters_by_incidence_range(sar_client):
    assert sar_client.get("/search?min_incidence=30").json()["context"]["returned"] == 2
    assert sar_client.get("/search?max_incidence=40").json()["context"]["returned"] == 2
    body = sar_client.get("/search?min_incidence=30&max_incidence=40").json()
    assert _returned_ids(body) == ["item-1"]


def test_search_filters_by_max_resolution(sar_client):
    # max_resolution keeps items at least this fine (range AND azimuth <= value).
    assert sar_client.get("/search?max_resolution=0.5").json()["context"]["returned"] == 2
    assert _returned_ids(sar_client.get("/search?max_resolution=0.3").json()) == ["item-0"]


def test_items_endpoint_filters_by_sar_properties(sar_client):
    path = f"/collections/{COLLECTION}/items?polarizations=VV"
    assert _returned_ids(sar_client.get(path).json()) == ["item-0"]
    path = f"/collections/{COLLECTION}/items?min_incidence=45"
    assert _returned_ids(sar_client.get(path).json()) == ["item-2"]


def test_sar_filters_survive_pagination(sar_client):
    page1 = sar_client.get("/search?min_incidence=30&limit=1").json()
    next_link = next(link for link in page1["links"] if link["rel"] == "next")
    assert "min_incidence=30" in next_link["href"]
    href = next_link["href"].replace("http://testserver", "")
    page2 = sar_client.get(href).json()
    assert _returned_ids(page2) == ["item-2"]


def test_post_search_query_extension_sar_properties(sar_client):
    # The proper STAC Query object form, using the namespaced property names.
    body = {
        "query": {
            "sar:polarizations": {"in": ["VV", "VH"]},
            "view:incidence_angle": {"lte": 30},
        }
    }
    assert _returned_ids(sar_client.post("/search", json=body).json()) == ["item-0"]
    # A gte/lte incidence range in one object.
    body = {"query": {"view:incidence_angle": {"gte": 30, "lte": 40}}}
    assert _returned_ids(sar_client.post("/search", json=body).json()) == ["item-1"]
    # Resolution lte.
    body = {"query": {"sar:resolution": {"lte": 0.3}}}
    assert _returned_ids(sar_client.post("/search", json=body).json()) == ["item-0"]


def test_post_search_top_level_sar_fields_override_query(sar_client):
    # A top-level field overrides the same field inside `query`.
    body = {
        "polarizations": ["HH"],
        "query": {"sar:polarizations": {"in": ["VV"]}},
    }
    assert _returned_ids(sar_client.post("/search", json=body).json()) == ["item-2"]
    # Top-level numeric fields also reach the index.
    body = {"min_incidence": 45}
    assert _returned_ids(sar_client.post("/search", json=body).json()) == ["item-2"]


def test_post_search_rejects_bad_sar_query(sar_client):
    assert (
        sar_client.post("/search", json={"query": {"view:incidence_angle": 30}}).status_code == 400
    )
    assert (
        sar_client.post("/search", json={"query": {"sar:resolution": {"gte": 1}}}).status_code
        == 400
    )
    assert sar_client.post("/search", json={"min_incidence": "wide"}).status_code == 400


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


# --------------------------------------------------------------------------
# On-demand render artifacts
# --------------------------------------------------------------------------

# A short, valid-enough PNG signature the fake renderers hand back. The routes
# treat render output as opaque bytes, so this never needs to be a real image.
FAKE_PNG = b"\x89PNG\r\n\x1a\nFAKE"


class RecordingRenderers:
    """Fake :class:`serve.Renderers` that records calls and returns FAKE_PNG.

    Lets the artifact routes be exercised with zero network and no ``viz``
    extra -- the whole point of the renderers being injectable.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []

    def _make(self, kind):
        def render(items, opts):
            ids = [items.id] if hasattr(items, "id") else [it.id for it in items]
            self.calls.append((kind, ids, dict(opts)))
            return FAKE_PNG

        return render

    def as_renderers(self) -> serve.Renderers:
        return serve.Renderers(
            quicklook=self._make("quicklook"),
            change=self._make("change"),
            timescan=self._make("timescan"),
            swipe=self._make("swipe"),
        )


@pytest.fixture
def recorder() -> RecordingRenderers:
    return RecordingRenderers()


@pytest.fixture
def art_client(index_path, recorder, tmp_path) -> TestClient:
    app = serve.build_app(
        index_path,
        renderers=recorder.as_renderers(),
        cache_dir=tmp_path / "artifacts",
    )
    return TestClient(app)


# ---- pure helpers (no server) --------------------------------------------


def test_artifact_cache_key_is_order_sensitive_on_items():
    opts = {"asset": "GEC", "max_size": 1024, "db": False}
    a = serve.artifact_cache_key("change", ["item-0", "item-1"], opts)
    b = serve.artifact_cache_key("change", ["item-1", "item-0"], opts)
    assert a != b, "frame order defines a distinct change composite"
    assert serve.artifact_cache_key("change", ["item-0", "item-1"], opts) == a


def test_artifact_cache_key_is_option_order_independent():
    a = serve.artifact_cache_key("quicklook", ["x"], {"asset": "GEC", "db": True, "max_size": 512})
    b = serve.artifact_cache_key("quicklook", ["x"], {"max_size": 512, "asset": "GEC", "db": True})
    assert a == b
    # A changed option changes the key.
    c = serve.artifact_cache_key("quicklook", ["x"], {"asset": "GEC", "db": False, "max_size": 512})
    assert c != a


def test_artifact_options_normalises_and_clamps():
    opts = serve.artifact_options({"max_size": 100_000, "db": 1})
    assert opts == {"asset": "GEC", "max_size": 8192, "db": True}
    assert serve.artifact_options(None) == {
        "asset": "GEC",
        "max_size": serve.ARTIFACT_MAX_SIZE,
        "db": False,
    }


def test_change_frames_selects_two_or_three():
    items = [f"i{n}" for n in range(5)]  # stand-ins; helper only counts/indexes
    assert serve.change_frames(items[:2]) == items[:2]
    assert serve.change_frames(items[:3]) == items[:3]
    three = serve.change_frames(items)  # 5 -> first/middle/last
    assert three == ["i0", "i2", "i4"]
    with pytest.raises(ValueError):
        serve.change_frames(items[:1])


def test_timescan_frames_requires_three_and_caps():
    with pytest.raises(ValueError):
        serve.timescan_frames(["a", "b"])
    many = [f"i{n}" for n in range(serve.ARTIFACT_MAX_FRAMES + 20)]
    picked = serve.timescan_frames(many)
    assert len(picked) == serve.ARTIFACT_MAX_FRAMES
    assert picked[0] == "i0" and picked[-1] == many[-1]


def test_resolve_items_preserves_requested_id_order(index_path):
    with CatalogIndex(index_path) as source:
        got = serve.resolve_items(source, ids=["item-2", "item-0"])
    assert [it.id for it in got] == ["item-2", "item-0"]


# ---- endpoints -----------------------------------------------------------


def test_quicklook_endpoint_renders_and_caches(art_client, recorder):
    resp = art_client.get("/artifacts/quicklook/item-1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-umbra-cache"] == "miss"
    assert resp.content == FAKE_PNG
    assert recorder.calls == [
        ("quicklook", ["item-1"], {"asset": "GEC", "max_size": 1024, "db": False})
    ]

    # Second identical request is served from disk -- renderer not called again.
    again = art_client.get("/artifacts/quicklook/item-1.png")
    assert again.status_code == 200
    assert again.headers["x-umbra-cache"] == "hit"
    assert again.content == FAKE_PNG
    assert len(recorder.calls) == 1


def test_quicklook_passes_options_through(art_client, recorder):
    art_client.get("/artifacts/quicklook/item-0.png?db=true&max_size=512&asset=CSI")
    assert recorder.calls[0][2] == {"asset": "CSI", "max_size": 512, "db": True}


def test_quicklook_unknown_item_is_404(art_client):
    assert art_client.get("/artifacts/quicklook/nope.png").status_code == 404


def test_thumbnail_endpoint_serves_baked_png(client, index_path):
    """A baked thumbnail is served straight from the index -- no render."""
    with CatalogIndex(index_path) as idx:
        assert idx.bake_thumbnails(lambda item: b"THUMB-" + item.id.encode()) == 3

    resp = client.get("/artifacts/thumbnail/item-1.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == b"THUMB-item-1"


def test_thumbnail_endpoint_unbaked_is_404(client):
    """A known item without a baked thumbnail is a 404 pointing at quicklook."""
    resp = client.get("/artifacts/thumbnail/item-0.png")
    assert resp.status_code == 404
    assert "bake-thumbnails" in resp.json()["detail"]


def test_thumbnail_endpoint_unknown_item_is_404(client):
    assert client.get("/artifacts/thumbnail/nope.png").status_code == 404


def test_thumbnail_endpoint_advertised_in_landing_when_artifacts_enabled():
    page = serve.landing_page("http://localhost:8000/", artifacts=True)
    rels = {link["rel"] for link in page["links"]}
    assert "thumbnail" in rels
    off = serve.landing_page("http://localhost:8000/", artifacts=False)
    assert "thumbnail" not in {link["rel"] for link in off["links"]}


def test_change_endpoint_two_dates(art_client, recorder):
    resp = art_client.post("/artifacts/change", json={"ids": ["item-0", "item-1"]})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    kind, ids, _ = recorder.calls[0]
    assert kind == "change"
    assert ids == ["item-0", "item-1"]


def test_change_endpoint_collapses_many_to_three(art_client, recorder):
    # A bbox query resolves all three items -> first/middle/last three-date RGB.
    resp = art_client.post("/artifacts/change", json={"bbox": [-69, 10, -67, 11]})
    assert resp.status_code == 200
    assert recorder.calls[0][1] == ["item-0", "item-1", "item-2"]


def test_change_endpoint_needs_two_acquisitions(art_client):
    resp = art_client.post("/artifacts/change", json={"ids": ["item-0"]})
    assert resp.status_code == 400
    assert "at least 2" in resp.json()["detail"]


def test_timescan_endpoint_renders(art_client, recorder):
    resp = art_client.post("/artifacts/timescan", json={"bbox": [-69, 10, -67, 11]})
    assert resp.status_code == 200
    kind, ids, _ = recorder.calls[0]
    assert kind == "timescan"
    assert ids == ["item-0", "item-1", "item-2"]


def test_timescan_endpoint_needs_three_acquisitions(art_client):
    resp = art_client.post("/artifacts/timescan", json={"ids": ["item-0", "item-1"]})
    assert resp.status_code == 400
    assert "at least 3" in resp.json()["detail"]


def test_swipe_frames_takes_temporal_endpoints():
    items = [f"i{n}" for n in range(5)]  # stand-ins; helper only counts/indexes
    assert serve.swipe_frames(items[:2]) == items[:2]
    # A query resolving many collapses to first and last (widest span).
    assert serve.swipe_frames(items) == ["i0", "i4"]
    with pytest.raises(ValueError):
        serve.swipe_frames(items[:1])


def test_swipe_endpoint_serves_html_from_its_own_cache(art_client, recorder):
    resp = art_client.post("/artifacts/swipe", json={"ids": ["item-0", "item-2"]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-umbra-cache"] == "miss"
    kind, ids, _ = recorder.calls[0]
    assert kind == "swipe"
    assert ids == ["item-0", "item-2"]

    # A repeat request is served from the HTML cache entry, renderer not re-run.
    again = art_client.post("/artifacts/swipe", json={"ids": ["item-0", "item-2"]})
    assert again.status_code == 200
    assert again.headers["x-umbra-cache"] == "hit"
    assert len(recorder.calls) == 1


def test_swipe_endpoint_needs_two_acquisitions(art_client):
    resp = art_client.post("/artifacts/swipe", json={"ids": ["item-0"]})
    assert resp.status_code == 400
    assert "at least 2" in resp.json()["detail"]


def test_landing_advertises_artifacts_when_enabled(art_client):
    rels = {link["rel"] for link in art_client.get("/").json()["links"]}
    assert {"quicklook", "change", "timescan", "swipe"} <= rels


def test_cors_headers_allow_cross_origin_calls(art_client):
    resp = art_client.get("/", headers={"Origin": "https://example.com"})
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_artifacts_can_be_disabled(index_path, tmp_path):
    app = serve.build_app(index_path, artifacts=False, cache_dir=tmp_path / "art")
    client = TestClient(app)
    assert client.get("/artifacts/quicklook/item-0.png").status_code == 404
    assert client.post("/artifacts/change", json={"ids": ["item-0", "item-1"]}).status_code == 404
    assert client.post("/artifacts/swipe", json={"ids": ["item-0", "item-1"]}).status_code == 404
    rels = {link["rel"] for link in client.get("/").json()["links"]}
    assert not ({"quicklook", "change", "timescan", "swipe"} & rels)


def test_missing_render_extra_maps_to_501(index_path, tmp_path):
    from umbra_py.exceptions import MissingDependencyError

    def boom(item, opts):
        raise MissingDependencyError("needs the 'viz' extra")

    renderers = serve.Renderers(quicklook=boom, change=boom, timescan=boom, swipe=boom)
    app = serve.build_app(index_path, renderers=renderers, cache_dir=tmp_path / "art")
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/artifacts/quicklook/item-0.png")
    assert resp.status_code == 501
    assert "viz" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Async render jobs (202 + poll; the disk cache is the result store)
# --------------------------------------------------------------------------


class _ManualExecutor:
    """An executor-shaped runner that defers submitted work until asked.

    Lets a test observe the ``queued``/``running`` states a real thread pool
    would produce, with none of the timing nondeterminism: ``submit`` just
    records the callable, and ``run_all`` runs the pending ones.
    """

    def __init__(self) -> None:
        self.pending: list = []

    def submit(self, fn):
        self.pending.append(fn)

    def run_all(self) -> None:
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.fixture
def inline_client(index_path, recorder, tmp_path) -> TestClient:
    """A client whose jobs run synchronously on submit (deterministic)."""
    app = serve.build_app(
        index_path,
        renderers=recorder.as_renderers(),
        cache_dir=tmp_path / "artifacts",
        job_executor=serve._InlineJobExecutor(),
    )
    return TestClient(app)


# ---- pure job document builder (no server) -------------------------------


def test_job_to_dict_queued_has_only_a_self_link():
    job = serve.RenderJob(
        id="abc", kind="change", cache_key="k", suffix="png", media_type="image/png"
    )
    doc = serve.job_to_dict(job, "http://localhost:8000/")
    assert doc["status"] == "queued"
    assert {link["rel"] for link in doc["links"]} == {"self"}
    assert "cache" not in doc and "error" not in doc


def test_job_to_dict_succeeded_adds_a_result_link():
    job = serve.RenderJob(
        id="abc",
        kind="swipe",
        cache_key="k",
        suffix="html",
        media_type="text/html; charset=utf-8",
        status=serve.JOB_SUCCEEDED,
        cached=True,
    )
    doc = serve.job_to_dict(job, "http://localhost:8000")
    result = next(link for link in doc["links"] if link["rel"] == "result")
    assert result["href"].endswith("/jobs/abc/result")
    assert result["type"] == "text/html; charset=utf-8"
    assert doc["cache"] == "hit"


def test_job_to_dict_failed_surfaces_the_error():
    job = serve.RenderJob(
        id="abc",
        kind="change",
        cache_key="k",
        suffix="png",
        media_type="image/png",
        status=serve.JOB_FAILED,
        error="boom",
    )
    doc = serve.job_to_dict(job, "http://localhost:8000")
    assert doc["error"] == "boom"
    assert {link["rel"] for link in doc["links"]} == {"self"}


# ---- endpoints -----------------------------------------------------------


def test_async_change_runs_and_serves_result(inline_client, recorder):
    resp = inline_client.post(
        "/artifacts/change", json={"ids": ["item-0", "item-1"], "async": True}
    )
    # The inline executor finishes the render during submit, so the job is born
    # succeeded and the artifact response is 200, not 202.
    assert resp.status_code == 200
    job = resp.json()
    assert job["kind"] == "change" and job["status"] == "succeeded"
    assert resp.headers["location"].endswith(f"/jobs/{job['id']}")
    assert recorder.calls[0][:2] == ("change", ["item-0", "item-1"])

    # Polling the job reports success with a result link.
    poll = inline_client.get(f"/jobs/{job['id']}").json()
    assert poll["status"] == "succeeded"
    result_link = next(link for link in poll["links"] if link["rel"] == "result")

    # Fetching the result serves the rendered bytes from the disk cache.
    result = inline_client.get(result_link["href"])
    assert result.status_code == 200
    assert result.headers["content-type"] == "image/png"
    assert result.content == FAKE_PNG
    # The render ran exactly once for the whole submit -> poll -> fetch flow.
    assert len(recorder.calls) == 1


def test_async_pending_job_then_result(index_path, recorder, tmp_path):
    executor = _ManualExecutor()
    app = serve.build_app(
        index_path,
        renderers=recorder.as_renderers(),
        cache_dir=tmp_path / "artifacts",
        job_executor=executor,
    )
    client = TestClient(app)

    # The work is deferred, so the request returns 202 with a queued job.
    resp = client.post("/artifacts/timescan", json={"bbox": [-69, 10, -67, 11], "async": True})
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    assert resp.json()["status"] == "queued"
    assert recorder.calls == []  # nothing rendered yet

    # While queued, the result endpoint refuses with 409 (poll, don't fetch).
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"
    assert client.get(f"/jobs/{job_id}/result").status_code == 409

    # Run the deferred render; the job flips to succeeded and the result serves.
    executor.run_all()
    assert client.get(f"/jobs/{job_id}").json()["status"] == "succeeded"
    result = client.get(f"/jobs/{job_id}/result")
    assert result.status_code == 200
    assert result.content == FAKE_PNG
    assert recorder.calls[0][0] == "timescan"


def test_async_already_cached_returns_succeeded_without_work(inline_client, recorder):
    body = {"ids": ["item-0", "item-1"]}
    # Populate the cache synchronously first.
    assert inline_client.post("/artifacts/change", json=body).status_code == 200
    assert len(recorder.calls) == 1

    # An async request for the same render finds the cache and does no new work.
    resp = inline_client.post("/artifacts/change", json={**body, "async": True})
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["status"] == "succeeded" and doc["cache"] == "hit"
    assert len(recorder.calls) == 1  # renderer not called again


def test_async_swipe_serves_html_result(inline_client):
    resp = inline_client.post("/artifacts/swipe", json={"ids": ["item-0", "item-2"], "async": True})
    assert resp.status_code == 200
    result = inline_client.get(f"/jobs/{resp.json()['id']}/result")
    assert result.status_code == 200
    assert result.headers["content-type"].startswith("text/html")
    assert result.content == FAKE_PNG


def test_async_validation_error_is_a_synchronous_400(inline_client):
    # Too few acquisitions is caught before any job is created.
    resp = inline_client.post("/artifacts/change", json={"ids": ["item-0"], "async": True})
    assert resp.status_code == 400
    assert "at least 2" in resp.json()["detail"]


def test_async_failed_render_reports_error_status(index_path, tmp_path):
    from umbra_py.exceptions import MissingDependencyError

    def boom(items, opts):
        raise MissingDependencyError("needs the 'viz' extra")

    renderers = serve.Renderers(quicklook=boom, change=boom, timescan=boom, swipe=boom)
    app = serve.build_app(
        index_path,
        renderers=renderers,
        cache_dir=tmp_path / "art",
        job_executor=serve._InlineJobExecutor(),
    )
    client = TestClient(app)
    resp = client.post("/artifacts/change", json={"ids": ["item-0", "item-1"], "async": True})
    # A failed render is not an HTTP error on submit -- it is a failed job.
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    poll = client.get(f"/jobs/{job_id}").json()
    assert poll["status"] == "failed"
    assert "viz" in poll["error"]
    # The result endpoint mirrors the synchronous path's 501 for a missing extra.
    result = client.get(f"/jobs/{job_id}/result")
    assert result.status_code == 501
    assert "viz" in result.json()["detail"]


def test_unknown_job_is_404(art_client):
    assert art_client.get("/jobs/does-not-exist").status_code == 404
    assert art_client.get("/jobs/does-not-exist/result").status_code == 404


def test_sync_default_is_unchanged_by_async_support(art_client, recorder):
    # Without the async flag, the endpoint still returns the artifact directly.
    resp = art_client.post("/artifacts/change", json={"ids": ["item-0", "item-1"]})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == FAKE_PNG
    assert resp.headers["x-umbra-cache"] == "miss"
