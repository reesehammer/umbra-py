"""Public-host abuse controls: client IP, concurrency, page size, bodies, links, geocode.

Local ``umbra serve`` (and local MCP, where COG streaming stays allowed) keeps
the previous limits. These tests never call the live host.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from umbra_py import mcp_server as ms
from umbra_py import serve
from umbra_py.exceptions import GeocodeError
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

DATA_DIR = Path(__file__).parent / "data"
_HAS_FASTAPI = importlib.util.find_spec("fastapi") is not None
_HAS_MCP = importlib.util.find_spec("mcp") is not None
_HAS_HTTPX = importlib.util.find_spec("httpx") is not None

pytestmark_public_app = pytest.mark.skipif(
    not (_HAS_FASTAPI and _HAS_MCP),
    reason="public app needs the serve and mcp extras",
)
pytestmark_serve = pytest.mark.skipif(not _HAS_FASTAPI, reason="serve extra")


def _index(tmp_path: Path) -> Path:
    sample = json.loads((DATA_DIR / "sample_item.json").read_text())
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        for i in range(3):
            doc = copy.deepcopy(sample)
            doc["id"] = f"item-{i}"
            href = (
                "https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/tasks/"
                f"Testville/2024-01-0{i + 1}-00-00-00_UMBRA-0{i + 1}/x{i}.stac.v2.json"
            )
            idx.add(UmbraItem.from_dict(doc, href=href))
    return path


def _client(app, **kwargs: object):
    from fastapi.testclient import TestClient

    return TestClient(app, **kwargs)


# --------------------------------------------------------------------------
# Rate limiter eviction and client address
# --------------------------------------------------------------------------


def test_rate_limiter_evicts_the_least_recent_client():
    clock = {"now": 0.0}
    limiter = serve.RateLimiter(limit=1, window_s=60, max_clients=2, time_fn=lambda: clock["now"])
    assert limiter.hit("a")[0] is True
    assert limiter.hit("b")[0] is True
    # ``a`` was seen again, so it is newer than ``b`` and must survive.
    assert limiter.hit("a")[0] is False
    assert limiter.hit("c")[0] is True
    assert limiter.hit("a")[0] is False
    # ``b`` was the one dropped, so it is a new client inside the old window.
    assert limiter.hit("b")[0] is True
    clock["now"] = 61.0
    assert limiter.hit("a")[0] is True


def test_forwarded_client_never_trusts_the_leftmost_hop():
    assert serve._forwarded_client("198.51.100.1, 203.0.113.9", 1) == "203.0.113.9"
    assert serve._forwarded_client("198.51.100.1, 203.0.113.9", 2) == "198.51.100.1"
    # Shorter than the configured hop count: the rightmost entry, still not
    # the left one when they differ.
    assert serve._forwarded_client("9.9.9.9, 203.0.113.9", 5) == "203.0.113.9"
    assert serve._forwarded_client("not-an-ip, 203.0.113.9", 1) == "203.0.113.9"
    assert serve._forwarded_client("not-an-ip", 1) is None


def test_public_client_ip_prefers_x_real_ip_and_ignores_loopback_headers(monkeypatch):
    monkeypatch.delenv("UMBRA_CLIENT_IP_HEADER", raising=False)
    headers = {
        "x-real-ip": "203.0.113.9",
        "x-forwarded-for": "198.51.100.1, 203.0.113.50",
    }
    assert serve.public_client_ip("10.0.0.8", headers) == "203.0.113.9"
    assert serve.public_client_ip("127.0.0.1", headers) == "127.0.0.1"
    assert serve.public_client_ip("::1", headers) == "::1"
    assert (
        serve.public_client_ip("10.0.0.8", {"x-forwarded-for": "1.1.1.1, 203.0.113.9"})
        == "203.0.113.9"
    )
    monkeypatch.setenv("UMBRA_CLIENT_IP_HEADER", "none")
    assert serve.public_client_ip("10.0.0.8", headers) == "10.0.0.8"


@pytestmark_public_app
def test_public_rate_limit_ignores_bearer_and_keeps_one_x_real_ip(tmp_path):
    index_path = _index(tmp_path)
    app = serve.build_app(index_path, public=True, rate_limit=2, cache_dir=tmp_path / "art")
    with _client(app) as client:
        first = {"x-real-ip": "203.0.113.9", "authorization": "Bearer aaa"}
        assert client.get("/search?limit=1", headers=first).status_code == 200
        rotated = {
            "x-real-ip": "203.0.113.9",
            "authorization": "Bearer bbb",
            "x-forwarded-for": "198.51.100.1",
        }
        assert client.get("/search?limit=1", headers=rotated).status_code == 200
        blocked = client.get(
            "/search?limit=1",
            headers={
                "x-real-ip": "203.0.113.9",
                "authorization": "Bearer ccc",
                "x-forwarded-for": "198.51.100.2, 203.0.113.8",
            },
        )
        assert blocked.status_code == 429
        other = client.get("/search?limit=1", headers={"x-real-ip": "203.0.113.10"})
        assert other.status_code == 200


@pytestmark_public_app
def test_public_rate_limit_uses_the_rightmost_forwarded_hop(tmp_path):
    index_path = _index(tmp_path)
    app = serve.build_app(index_path, public=True, rate_limit=2, cache_dir=tmp_path / "art")
    with _client(app) as client:
        for leftmost in ("198.51.100.1", "198.51.100.2"):
            resp = client.get(
                "/search?limit=1",
                headers={"x-forwarded-for": f"{leftmost}, 203.0.113.9"},
            )
            assert resp.status_code == 200
        blocked = client.get(
            "/search?limit=1",
            headers={"x-forwarded-for": "203.0.113.50, 203.0.113.9"},
        )
        assert blocked.status_code == 429
        fresh = client.get(
            "/search?limit=1",
            headers={"x-forwarded-for": "203.0.113.9, 203.0.113.10"},
        )
        assert fresh.status_code == 200


@pytestmark_public_app
def test_loopback_public_ignores_forwarded_headers(tmp_path):
    index_path = _index(tmp_path)
    app = serve.build_app(index_path, public=True, rate_limit=2, cache_dir=tmp_path / "art")
    with _client(app, client=("127.0.0.1", 12345)) as client:
        assert (
            client.get("/search?limit=1", headers={"x-real-ip": "203.0.113.9"}).status_code == 200
        )
        assert (
            client.get(
                "/search?limit=1",
                headers={"x-forwarded-for": "203.0.113.10", "x-real-ip": "203.0.113.11"},
            ).status_code
            == 200
        )
        blocked = client.get("/search?limit=1", headers={"x-real-ip": "203.0.113.12"})
        assert blocked.status_code == 429


@pytestmark_serve
def test_local_rate_limit_still_separates_bearer_tokens(tmp_path):
    index_path = _index(tmp_path)
    app = serve.build_app(index_path, artifacts=False, rate_limit=1, cache_dir=tmp_path / "art")
    with _client(app) as client:
        assert (
            client.get("/search?limit=1", headers={"authorization": "Bearer aaa"}).status_code
            == 200
        )
        assert (
            client.get("/search?limit=1", headers={"authorization": "Bearer aaa"}).status_code
            == 429
        )
        assert (
            client.get("/search?limit=1", headers={"authorization": "Bearer bbb"}).status_code
            == 200
        )


# --------------------------------------------------------------------------
# Concurrency and SQLite interrupt
# --------------------------------------------------------------------------


def test_query_cancel_interrupts_sqlite(tmp_path):
    path = tmp_path / "cancel.db"
    CatalogIndex(path).close()
    cancelled = threading.Event()
    started = threading.Event()
    errors: list[BaseException] = []
    holder: dict[str, CatalogIndex] = {}

    def run() -> None:
        idx = CatalogIndex(path)
        holder["idx"] = idx
        idx.arm_query_cancel(cancelled)
        started.set()
        try:
            idx._conn.execute(
                "WITH RECURSIVE c(x) AS ("
                "SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500000000"
                ") SELECT sum(x) FROM c"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            errors.append(exc)
        finally:
            idx.disarm_query_cancel()
            idx.close()

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(2)
    time.sleep(0.05)
    cancelled.set()
    holder["idx"].interrupt_query()
    worker.join(2)
    assert not worker.is_alive()
    assert errors


def test_client_in_flight_is_per_client():
    gate = serve.ClientInFlight(1)
    assert gate.acquire("ip:a") is True
    assert gate.acquire("ip:a") is False
    assert gate.acquire("ip:b") is True
    gate.release("ip:a")
    assert gate.acquire("ip:a") is True


@pytestmark_public_app
@pytest.mark.skipif(not _HAS_HTTPX, reason="httpx")
def test_public_client_concurrency_rejects_a_second_read(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("UMBRA_CLIENT_CONCURRENCY", "1")
    started = threading.Event()
    release = threading.Event()

    def block(source, **kwargs):
        started.set()
        assert release.wait(5)
        return [], False

    monkeypatch.setattr(serve, "run_search", block)
    app = serve.build_app(_index(tmp_path), public=True, rate_limit=0, cache_dir=tmp_path / "art")

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.get("/search", params={"limit": 1}))
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.02)
            assert started.is_set()
            second = await client.get("/search", params={"limit": 1})
            release.set()
            first_response = await first
            third = await client.get("/search", params={"limit": 1})
            return first_response, second, third

    first, second, third = asyncio.run(_run())
    assert first.status_code == 200
    assert second.status_code == 429
    assert "in flight" in second.json()["detail"]
    assert third.status_code == 200


@pytestmark_public_app
@pytest.mark.skipif(not _HAS_HTTPX, reason="httpx")
def test_public_timeout_interrupts_the_query_and_releases_the_slot(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("UMBRA_REQUEST_TIMEOUT_S", "0.3")
    monkeypatch.setenv("UMBRA_DB_CONCURRENCY", "1")
    monkeypatch.setenv("UMBRA_CLIENT_CONCURRENCY", "2")

    def slow(source, **kwargs):
        source._conn.execute(
            "WITH RECURSIVE c(x) AS ("
            "SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500000000"
            ") SELECT sum(x) FROM c"
        ).fetchone()
        return [], False

    monkeypatch.setattr(serve, "run_search", slow)
    app = serve.build_app(_index(tmp_path), public=True, rate_limit=0, cache_dir=tmp_path / "art")

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/search", params={"limit": 1})
            second = None
            for _ in range(40):
                second = await client.get("/search", params={"limit": 1})
                if second.status_code == 503 and second.json().get("error") == "Timeout":
                    return first, second
                await asyncio.sleep(0.05)
            return first, second

    first, second = asyncio.run(_run())
    assert first.status_code == 503
    assert first.json()["error"] == "Timeout"
    assert second is not None
    assert second.status_code == 503
    assert second.json().get("error") == "Timeout"


# --------------------------------------------------------------------------
# Page size, body limit, POST validation, link origin
# --------------------------------------------------------------------------


def _seen_limits(monkeypatch):
    seen: list[int] = []

    def fake(source, **kwargs):
        seen.append(kwargs["limit"])
        return [], True

    monkeypatch.setattr(serve, "run_search", fake)
    return seen


@pytestmark_public_app
def test_public_search_clamps_limit_and_pages_by_the_cap(tmp_path, monkeypatch):
    seen = _seen_limits(monkeypatch)
    app = serve.build_app(_index(tmp_path), public=True, rate_limit=0, cache_dir=tmp_path / "art")
    with _client(app) as client:
        resp = client.get("/search?limit=10000")
        assert resp.status_code == 200
        assert seen[-1] == serve.PUBLIC_SEARCH_LIMIT
        nxt = next(link for link in resp.json()["links"] if link["rel"] == "next")
        assert "token=500" in nxt["href"]
        items = client.get(f"/collections/{serve.COLLECTION_ID}/items?limit=10000")
        assert items.status_code == 200
        assert seen[-1] == serve.PUBLIC_SEARCH_LIMIT
        posted = client.post("/search", json={"limit": 10000})
        assert posted.status_code == 200
        assert seen[-1] == serve.PUBLIC_SEARCH_LIMIT
        body = next(link["body"] for link in posted.json()["links"] if link["rel"] == "next")
        assert body["token"] == 500
        assert body["limit"] == 10000
        default = client.get("/search")
        assert default.status_code == 200
        assert seen[-1] == serve.DEFAULT_LIMIT


@pytestmark_serve
def test_local_search_keeps_the_large_page_size(tmp_path, monkeypatch):
    seen = _seen_limits(monkeypatch)
    app = serve.build_app(_index(tmp_path), artifacts=False, cache_dir=tmp_path / "art")
    with _client(app) as client:
        resp = client.get("/search?limit=600")
        assert resp.status_code == 200
        assert seen[-1] == 600
        big = b'{"limit":1}' + b" " * serve.PUBLIC_MAX_BODY_BYTES
        posted = client.post("/search", content=big, headers={"content-type": "application/json"})
        assert posted.status_code != 413


@pytestmark_public_app
def test_public_body_over_one_mib_is_413_and_counts(tmp_path):
    app = serve.build_app(_index(tmp_path), public=True, rate_limit=2, cache_dir=tmp_path / "art")
    big = b'{"limit":1}' + b" " * serve.PUBLIC_MAX_BODY_BYTES
    with _client(app) as client:
        first = client.post("/search", content=big, headers={"content-type": "application/json"})
        assert first.status_code == 413
        assert first.json()["error"] == "RequestEntityTooLarge"
        ok = client.post("/search", json={"limit": 1})
        assert ok.status_code == 200
        second = client.post("/search", content=big, headers={"content-type": "application/json"})
        assert second.status_code == 429


def test_chunked_body_over_the_cap_is_413():
    messages: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    wrapped = serve.BodyLimitMiddleware(app, max_bytes=8)
    chunks = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"67890", "more_body": False},
    ]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        messages.append(message)

    asyncio.run(
        wrapped(
            {"type": "http", "method": "POST", "headers": [], "client": ("203.0.113.9", 1)},
            receive,
            send,
        )
    )
    assert messages[0]["status"] == 413


@pytestmark_serve
def test_post_search_rejects_malformed_bodies(tmp_path):
    app = serve.build_app(_index(tmp_path), artifacts=False, cache_dir=tmp_path / "art")
    cases: list[tuple[str, dict | None, bytes | None]] = [
        ("token abc", {"token": "abc"}, None),
        ("token negative", {"token": -5}, None),
        ("token huge", {"token": 10**30}, None),
        ("limit abc", {"limit": "abc"}, None),
        ("limit 1e400", {"limit": "1e400"}, None),
        ("ids scalar", {"ids": 5}, None),
        ("ids dict", {"ids": [{"a": 1}]}, None),
        ("ids 40000", {"ids": [f"x{i}" for i in range(40000)]}, None),
        ("collections scalar", {"collections": 5}, None),
        ("query string", {"query": "x"}, None),
        ("fuzzy object", {"fuzzy": {"a": 1}, "area": "x"}, None),
        ("area object", {"area": {"a": 1}}, None),
        ("bbox bad", {"bbox": [1, 2, "a", 4]}, None),
        ("datetime bad", {"datetime": "not/a/date"}, None),
        ("product_types huge", {"product_types": ["GEC"] * 50}, None),
        ("bbox nan", None, b'{"bbox":[NaN,0,1,1]}'),
        ("deep json", None, (b"[" * 5000) + (b"]" * 5000)),
    ]
    with _client(app, raise_server_exceptions=False) as client:
        for label, body, raw in cases:
            if raw is None:
                resp = client.post("/search", json=body)
            else:
                resp = client.post(
                    "/search",
                    content=raw,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 400, (label, resp.status_code, resp.text[:240])
        empty_limit = client.post("/search", json={"limit": None})
        assert empty_limit.status_code == 200
        assert len(empty_limit.json()["features"]) <= serve.DEFAULT_LIMIT


@pytestmark_public_app
def test_stac_links_use_the_configured_origin(tmp_path, monkeypatch):
    app = serve.build_app(_index(tmp_path), public=True, rate_limit=0, cache_dir=tmp_path / "art")
    with _client(app) as client:
        reflected = client.get("/", headers={"host": "evil.example"}).json()
        assert reflected["links"][0]["href"].startswith("http://evil.example")
        monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "example.up.railway.app")
        railway = client.get("/", headers={"host": "evil.example"}).json()
        assert railway["links"][0]["href"].startswith("https://example.up.railway.app")
        assert "evil.example" not in railway["links"][0]["href"]
        monkeypatch.setenv("UMBRA_PUBLIC_BASE_URL", "https://api.umbra-py.space")
        custom = client.get("/search?limit=1", headers={"host": "evil.example"}).json()
        assert all(
            link["href"].startswith("https://api.umbra-py.space") for link in custom["links"]
        )
        assert "evil.example" not in json.dumps(custom["links"])


@pytestmark_serve
def test_local_links_stay_on_the_request_unless_an_origin_is_configured(tmp_path, monkeypatch):
    app = serve.build_app(_index(tmp_path), artifacts=False, cache_dir=tmp_path / "art")
    with _client(app) as client:
        href = client.get("/").json()["links"][0]["href"]
        assert href.startswith("http://testserver")
        monkeypatch.setenv("UMBRA_PUBLIC_BASE_URL", "https://maps.example")
        moved = client.get("/").json()["links"][0]["href"]
        assert moved.startswith("https://maps.example")


# --------------------------------------------------------------------------
# MCP search_catalog clamp and geocode throttle
# --------------------------------------------------------------------------


class _Empty:
    def search(self, **kwargs):
        _Empty.kwargs = kwargs
        return iter(())

    def close(self) -> None:
        return None


def test_public_search_catalog_clamps_only_above_100(monkeypatch):
    monkeypatch.setattr(ms, "_search_source", lambda *a, **k: (_Empty(), False))
    ms.configure(cog_streaming=False)
    default = ms.search_catalog(local=False)
    assert _Empty.kwargs["limit"] == 20
    assert "note" not in default
    twenty = ms.search_catalog(limit=20, local=False)
    assert _Empty.kwargs["limit"] == 20
    assert "note" not in twenty
    clamped = ms.search_catalog(limit=500, local=False)
    assert _Empty.kwargs["limit"] == 100
    assert "next page" in clamped["note"]
    ms.configure(cog_streaming=True)
    local = ms.search_catalog(limit=500, local=False)
    assert _Empty.kwargs["limit"] == 500
    assert "note" not in local


def test_public_semantic_miss_still_notes_a_clamped_limit(monkeypatch):
    monkeypatch.setattr(ms, "_resolve_semantic_area", lambda *a, **k: (None, []))
    ms.configure(cog_streaming=False)
    out = ms.search_catalog(area="no such site", semantic=True, limit=500, local=False)
    assert out["count"] == 0
    assert "note" in out
    ms.configure(cog_streaming=True)
    local = ms.search_catalog(area="no such site", semantic=True, limit=500, local=False)
    assert "note" not in local


def test_public_geocode_caches_and_throttles(monkeypatch):
    ms.configure(cog_streaming=False)
    ms._public_geocode.clear()
    clock = {"now": 50.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(ms._public_geocode, "_time", lambda: clock["now"])
    monkeypatch.setattr(ms._public_geocode, "_sleep", sleep)
    calls: list[str] = []

    def fake(query: str):
        calls.append(query)
        return ((0.0, 0.0, 1.0, 1.0), query.strip())

    monkeypatch.setattr(ms, "_nominatim_geocode", fake)
    assert ms.geocode_place("Paris")["display_name"] == "Paris"
    assert ms.geocode_place(" paris ")["bbox"] == [0.0, 0.0, 1.0, 1.0]
    assert calls == ["Paris"]
    assert slept == []
    ms.geocode_place("Lyon")
    assert calls == ["Paris", "Lyon"]
    assert slept == [1.0]

    monkeypatch.setattr(ms, "_search_source", lambda *a, **k: (_Empty(), False))
    ms.search_catalog(place="Paris", local=False)
    assert calls == ["Paris", "Lyon"]


def test_local_geocode_is_not_throttled(monkeypatch):
    ms.configure(cog_streaming=True)
    calls: list[str] = []
    slept: list[float] = []

    def fake(query: str):
        calls.append(query)
        return ((0.0, 1.0, 2.0, 3.0), query)

    monkeypatch.setattr(ms, "_nominatim_geocode", fake)
    monkeypatch.setattr(ms._public_geocode, "_sleep", lambda seconds: slept.append(seconds))
    ms.geocode_place("A")
    ms.geocode_place("B")
    assert calls == ["A", "B"]
    assert slept == []


def test_public_geocode_does_not_cache_a_failure_and_drops_the_oldest(monkeypatch):
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    gate = ms.PublicGeocodeGate(
        max_size=2,
        min_interval=1.0,
        time_fn=lambda: clock["now"],
        sleep_fn=sleep,
    )
    calls: list[str] = []

    def fake(query: str):
        calls.append(query)
        if query == "missing":
            raise GeocodeError("no place")
        return ((0.0, 0.0, 1.0, 1.0), query)

    monkeypatch.setattr(ms, "_nominatim_geocode", fake)
    with pytest.raises(GeocodeError):
        gate.resolve("missing")
    with pytest.raises(GeocodeError):
        gate.resolve("missing")
    assert calls == ["missing", "missing"]
    gate.resolve("a")
    gate.resolve("b")
    gate.resolve("a")
    assert calls == ["missing", "missing", "a", "b"]
    gate.resolve("c")
    # ``a`` was used again, so ``b`` is the entry the cache drops.
    gate.resolve("b")
    assert calls[-2:] == ["c", "b"]
