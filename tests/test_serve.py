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
