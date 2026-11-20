"""Usage counter: what is counted, what is published, and what is never stored.

Offline. The HTTP cases skip when FastAPI is not installed, which is the
core CI job; the filter, cache, threshold, coarsening, restart, and privacy
cases do not need it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from umbra_py.usage_stats import (
    MIN_DISTINCT_CLIENTS,
    TOP_PLACES,
    USAGE_PLACE_KEY,
    SearchUsageMiddleware,
    ToolUsageCount,
    UsageStats,
    coarse_place,
    coarsen_label,
    current_recorder,
    index_facts,
    install_recorder,
    read_place_anchors,
    remember_search_place,
    request_counts,
    stats_path_for,
)

DATA_DIR = Path(__file__).parent / "data"
UA = {"user-agent": "Mozilla/5.0"}
SECRET_IP = "203.0.113.77"
SECRET_UA = "Mozilla/5.0 (SecretAgent/9.1)"
RAW_PLACE = "123 Secret Street, Springfield, Illinois, United States"
SECRET_COORD = "12.345678"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _hosts(n: int, prefix: str = "203.0.113.") -> list[str]:
    return [f"{prefix}{i}" for i in range(1, n + 1)]


def _stats(tmp_path: Path, clock: Clock | None = None) -> UsageStats:
    return UsageStats(tmp_path / "usage-stats.json", clock=clock or Clock(1_700_000_000.0))


# --------------------------------------------------------------------------
# Traffic filter
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status", "headers", "counted"),
    [
        ("GET", "/search", 200, UA, True),
        ("POST", "/search", 201, UA, True),
        ("GET", "/search/", 200, UA, True),
        ("HEAD", "/search", 200, UA, False),
        ("OPTIONS", "/search", 200, UA, False),
        ("GET", "/livez", 200, UA, False),
        ("GET", "/healthz", 200, UA, False),
        ("GET", "/healthz/live", 200, UA, False),
        ("GET", "/readyz", 200, UA, False),
        ("GET", "/no-such-path", 404, UA, False),
        ("GET", "/.env", 200, UA, False),
        ("GET", "/.git/config", 404, UA, False),
        ("GET", "/wp-admin", 404, UA, False),
        ("GET", "/search", 404, UA, False),
        ("GET", "/search", 429, UA, False),
        ("GET", "/search", 500, UA, False),
        ("GET", "/search", True, UA, False),
        ("GET", "/search", 200, {"user-agent": "Mozilla/5.0", "x-umbra-smoke": "1"}, False),
        ("GET", "/search", 200, {"user-agent": "Mozilla/5.0", "X-Umbra-Smoke": " yes "}, False),
        ("GET", "/search", 200, {"user-agent": "UptimeRobot/2.0"}, False),
        ("GET", "/search", 200, {"user-agent": "umbra-py-healthz-monitor"}, False),
        ("GET", "/search", 200, {"user-agent": "umbra-py-mcp-smoke"}, False),
        ("GET", "/search", 200, {"user-agent": "Googlebot/2.1"}, False),
        ("GET", "/search", 200, {"user-agent": ""}, False),
        ("GET", "/search", 200, {}, False),
        ("GET", "/collections/umbra-open-data/items", 200, UA, False),
        ("GET", "/SEARCH", 200, UA, False),
        ("GET", "/search/../.env", 200, UA, False),
    ],
)
def test_request_counts_keeps_real_searches_only(method, path, status, headers, counted):
    assert request_counts(method, path, status, headers) is counted


def test_ignore_ua_env_excludes_a_configured_substring(monkeypatch):
    monkeypatch.setenv("UMBRA_STATS_IGNORE_UA", "internal-probe, lab-crawler")
    headers = {"user-agent": "internal-probe/1"}
    assert request_counts("GET", "/search", 200, headers) is False
    assert request_counts("GET", "/search", 200, UA) is True


def test_observe_http_counts_the_scope_the_middleware_sees(tmp_path):
    stats = _stats(tmp_path)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/search",
        "headers": [(b"user-agent", b"Mozilla/5.0")],
        "client": (SECRET_IP, 443),
    }
    stats.observe_http(scope, 200)
    stats.observe_http({**scope, "path": "/livez"}, 200)
    stats.observe_http({**scope, "headers": [(b"user-agent", b"UptimeRobot")]}, 200)
    stats.observe_http(
        {**scope, "headers": [(b"user-agent", b"Mozilla/5.0"), (b"x-umbra-smoke", b"1")]},
        200,
    )
    stats.observe_http(scope, 429)
    assert stats.searches_served == 1


def test_a_stats_error_does_not_fail_the_response():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    stats = UsageStats(None, clock=Clock(1_700_000_000.0))

    def boom(scope, status):
        raise RuntimeError("stats disk")

    stats.observe_http = boom  # type: ignore[method-assign]
    middleware = SearchUsageMiddleware(app, stats)
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/search",
                "headers": [(b"user-agent", b"Mozilla/5.0")],
            },
            receive,
            send,
        )
    )
    assert messages[0]["status"] == 200


# --------------------------------------------------------------------------
# Hourly cache
# --------------------------------------------------------------------------


def test_public_payload_is_recomputed_at_most_once_an_hour(tmp_path):
    clock = Clock(1_700_000_000.0)
    calls = {"n": 0}

    def facts():
        calls["n"] += 1
        return {"scenes_indexed": 3, "thumbnails": 0, "index_built_at": None}

    stats = UsageStats(tmp_path / "usage-stats.json", clock=clock, facts=facts)
    stats.record_search(client="203.0.113.1", place="Tokyo")
    first = stats.public_payload()
    stats.record_search(client="203.0.113.2", place="Tokyo")
    clock.now += 3599
    second = stats.public_payload()
    assert second == first
    assert second["searches_served"] == 1
    assert calls["n"] == 1
    clock.now += 1
    third = stats.public_payload()
    assert third["searches_served"] == 2
    assert third["generated_at"] != first["generated_at"]
    assert calls["n"] == 2


# --------------------------------------------------------------------------
# Distinct clients and the publish threshold
# --------------------------------------------------------------------------


def test_a_place_is_published_only_at_three_distinct_clients(tmp_path):
    stats = _stats(tmp_path)
    hosts = _hosts(2)
    for host in hosts:
        stats.record_search(client=host, place="Tokyo")
    assert stats.distinct_clients("Tokyo") == 2
    assert stats.public_payload()["top_places"] == []
    stats.record_search(client="203.0.113.3", place="Tokyo")
    stats.record_search(client="203.0.113.3", place="Tokyo")
    # The cache is an hour old only in wall time; this payload was not cached
    # with the place in it because the first read happened before the third
    # client. Advance the clock so the new total is what gets published.
    assert stats.distinct_clients("Tokyo") == MIN_DISTINCT_CLIENTS
    clock = stats._clock
    clock.now += 3600  # type: ignore[attr-defined]
    payload = stats.public_payload()
    assert payload["top_places"] == [{"place": "Tokyo", "clients": 3}]
    stats.record_search(client="198.51.100.1", place="Oslo")
    stats.record_search(client="198.51.100.2", place="Oslo")
    clock.now += 3600  # type: ignore[attr-defined]
    places = {row["place"] for row in stats.public_payload()["top_places"]}
    assert places == {"Tokyo"}


def test_one_client_across_three_days_is_not_three_clients(tmp_path):
    clock = Clock(1_700_000_000.0)
    stats = UsageStats(tmp_path / "usage-stats.json", clock=clock)
    salt = stats._salt
    for _ in range(3):
        stats.record_search(client="203.0.113.1", place="Tokyo")
        clock.now += 86_400
    assert stats._salt != salt
    assert stats.distinct_clients("Tokyo") == 1
    assert stats.searches_served == 3
    assert stats.public_payload()["top_places"] == []


def test_top_places_keeps_ten_highest_counts(tmp_path):
    clock = Clock(1_700_000_000.0)
    stats = UsageStats(tmp_path / "usage-stats.json", clock=clock)
    for index in range(12):
        name = f"City{chr(65 + index)}"
        for host in _hosts(3, prefix=f"198.51.{index}."):
            stats.record_search(client=host, place=name)
    for host in _hosts(5, prefix="203.0.114."):
        stats.record_search(client=host, place="CityZed")
    rows = stats.public_payload()["top_places"]
    assert [row["place"] for row in rows] == ["CityZed", *[f"City{chr(65 + i)}" for i in range(9)]]
    assert all(row["clients"] >= MIN_DISTINCT_CLIENTS for row in rows)
    assert len(rows) == TOP_PLACES


# --------------------------------------------------------------------------
# Place coarsening
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Tokyo", "Tokyo"),
        ("Reykjavík, Capital Region, Iceland", "Reykjavík, Iceland"),
        ("Austin, Travis County, Texas, United States", "Austin, United States"),
        (RAW_PLACE, "Springfield, United States"),
        ("Main St, Springfield, United States", "Springfield, United States"),
        ("St. Louis, Missouri, United States", "St. Louis, United States"),
        ("ship_detection_testdata", None),
        ("https://evil.example/admin", None),
        ("203.0.113.50", None),
        ("", None),
        (None, None),
    ],
)
def test_coarsen_label_keeps_a_city_and_country(raw, expected):
    assert coarsen_label(raw) == expected


def test_bbox_and_intersects_use_the_nearest_baked_label():
    anchors = [("Reykjavík, Capital Region, Iceland", 10.49, -68.007)]
    named = coarse_place(bbox=(-68.02, 10.47, -67.99, 10.51), anchors=anchors)
    assert named == "Reykjavík, Iceland"
    point = {"type": "Point", "coordinates": [-68.007, 10.49]}
    assert coarse_place(intersects=point, anchors=anchors) == "Reykjavík, Iceland"
    assert coarse_place(bbox=(-68.02, 10.47, -67.99, 10.51), anchors=[]) is None
    assert coarse_place(bbox=(-10.0, 40.0, -9.5, 40.4), anchors=anchors) is None
    assert coarse_place(bbox=(-20.0, 10.0, 20.0, 40.0), anchors=anchors) is None


def test_a_geocoded_label_wins_over_the_raw_place_and_the_box():
    anchors = [("Reykjavík, Capital Region, Iceland", 10.49, -68.007)]
    named = coarse_place(
        place=RAW_PLACE,
        resolved_place="Tokyo",
        bbox=(-68.02, 10.47, -67.99, 10.51),
        anchors=anchors,
    )
    assert named == "Tokyo"


def test_remember_search_place_keeps_only_the_coarse_name():
    scope: dict = {}
    remember_search_place(
        scope,
        place=RAW_PLACE,
        bbox=(-89.654321, 39.78, -89.65, 39.79),
    )
    assert scope[USAGE_PLACE_KEY] == "Springfield, United States"
    assert "Secret" not in scope[USAGE_PLACE_KEY]
    assert SECRET_COORD not in json.dumps(scope)


def test_place_anchors_come_from_baked_index_labels(tmp_path):
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    doc = json.loads((DATA_DIR / "sample_item.json").read_text())
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as index:
        index.add(UmbraItem.from_dict(doc, href="https://example.test/a.stac.v2.json"))
        index.bake_places(lambda lat, lon: "Reykjavík, Capital Region, Iceland")
        anchors = index.place_anchors()
    assert anchors[0][0] == "Reykjavík, Capital Region, Iceland"
    assert abs(anchors[0][1] - 10.49) < 0.05
    assert abs(anchors[0][2] + 68.0) < 0.05
    assert read_place_anchors(path) == anchors
    facts = index_facts(path)
    assert facts["scenes_indexed"] == 1
    assert facts["thumbnails"] == 0
    assert facts["index_built_at"] is None
    assert index_facts(tmp_path / "missing.db")["scenes_indexed"] is None
    assert stats_path_for(path) == path.with_name("usage-stats.json")


# --------------------------------------------------------------------------
# Persistence across a restart, and a volume that cannot be written
# --------------------------------------------------------------------------


def test_counts_survive_a_restart_without_inflating_clients(tmp_path):
    clock = Clock(1_700_000_000.0)
    path = tmp_path / "usage-stats.json"
    stats = UsageStats(path, clock=clock)
    hosts = _hosts(3)
    for host in hosts:
        stats.record_search(client=host, place="Tokyo")
    stats.flush()
    since = stats.counting_since
    salt = bytes(stats._salt)
    restarted = UsageStats(path, clock=clock)
    assert restarted.searches_served == 3
    assert restarted.counting_since == since
    assert restarted.distinct_clients("Tokyo") == 3
    assert restarted._salt != salt
    for host in hosts:
        restarted.record_search(client=host, place="Tokyo")
    assert restarted.distinct_clients("Tokyo") == 3
    assert restarted.searches_served == 6
    restarted.record_search(client="203.0.113.99", place="Oslo")
    assert restarted.distinct_clients("Oslo") == 1


def test_a_corrupt_or_poisoned_file_does_not_invent_a_count(tmp_path):
    path = tmp_path / "usage-stats.json"
    path.write_text("{", encoding="utf-8")
    broken = UsageStats(path, clock=Clock(1_700_000_000.0))
    assert broken.searches_served == 0
    path.write_text(
        json.dumps(
            {
                "version": 99,
                "counting_since": "2020-01-01T00:00:00Z",
                "searches_served": 1000,
                "places": {"Tokyo": 9},
            }
        ),
        encoding="utf-8",
    )
    unknown = UsageStats(path, clock=Clock(1_700_000_000.0))
    assert unknown.searches_served == 0
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "counting_since": "2026-11-20T00:00:00Z",
                "searches_served": 4,
                "places": {"203.0.113.50": 9, "Tokyo": 3, "Main Street": 5},
            }
        ),
        encoding="utf-8",
    )
    loaded = UsageStats(path, clock=Clock(1_700_000_000.0))
    assert loaded.searches_served == 4
    assert loaded.distinct_clients("Tokyo") == 3
    assert loaded.distinct_clients("203.0.113.50") == 0
    assert loaded.distinct_clients("Main Street") == 0


def test_an_unwritable_volume_still_answers(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    stats = UsageStats(blocker / "usage-stats.json", clock=Clock(1_700_000_000.0))
    stats.record_search(client="203.0.113.9", place="Tokyo")
    stats.flush()
    payload = stats.public_payload()
    assert payload["searches_served"] == 1
    assert stats.searches_served == 1


# --------------------------------------------------------------------------
# Privacy: the payload and the file
# --------------------------------------------------------------------------


def test_no_ip_user_agent_or_raw_query_is_stored(tmp_path):
    clock = Clock(1_700_000_000.0)
    stats = UsageStats(tmp_path / "usage-stats.json", clock=clock)
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/search",
        "headers": [(b"user-agent", SECRET_UA.encode())],
        "client": (SECRET_IP, 443),
        "query_string": f"area={RAW_PLACE}&lon={SECRET_COORD}".encode(),
    }
    remember_search_place(scope, place=RAW_PLACE, bbox=(-89.654321, 39.78, -89.65, 39.79))
    for index, host in enumerate(_hosts(3)):
        one = dict(scope)
        one["client"] = (host if index else SECRET_IP, 443)
        if index:
            remember_search_place(one, place="Springfield, United States")
        stats.observe_http(one, 200)
    stats.flush()
    text = (tmp_path / "usage-stats.json").read_text(encoding="utf-8")
    document = json.loads(text)
    assert set(document) == {"version", "counting_since", "searches_served", "places"}
    assert document["places"] == {"Springfield, United States": 3}
    blob = text + json.dumps(stats.public_payload())
    for secret in (SECRET_IP, SECRET_UA, "Secret", SECRET_COORD, "89.654321", stats._salt.hex()):
        assert secret not in blob


# --------------------------------------------------------------------------
# MCP counting stays outside the tool signature
# --------------------------------------------------------------------------


def _tool_ctx(
    host: str,
    *,
    method: str = "tools/call",
    arguments: dict | None = None,
    headers: dict | None = None,
):
    if headers is None:
        headers = {"user-agent": "claude-code/2.1"}
    return SimpleNamespace(
        method=method,
        params={"name": "search_catalog", "arguments": arguments or {}},
        request=SimpleNamespace(client=(host, 1), headers=headers),
        session=SimpleNamespace(client_params=None),
    )


def test_mcp_middleware_counts_a_successful_call_and_returns_it(tmp_path):
    stats = _stats(tmp_path)
    stats.enable()
    middleware = ToolUsageCount(stats)
    result = {
        "structuredContent": {
            "resolved_place": "Austin, Travis County, Texas, United States",
            "count": 1,
        },
        "isError": False,
    }

    async def call_next(ctx):
        return result

    ctx = _tool_ctx(
        "198.51.100.8",
        arguments={"area": f"ships near {SECRET_COORD}", "place": RAW_PLACE},
    )
    sent = asyncio.run(middleware(ctx, call_next))
    assert sent is result
    assert stats.searches_served == 1
    assert stats.distinct_clients("Austin, United States") == 1
    stats.flush()
    text = (tmp_path / "usage-stats.json").read_text(encoding="utf-8")
    assert "Secret" not in text
    assert SECRET_COORD not in text
    assert "198.51.100.8" not in text


def test_mcp_middleware_skips_lists_errors_smoke_and_bots(tmp_path):
    stats = _stats(tmp_path)
    stats.enable()
    middleware = ToolUsageCount(stats)

    async def ok(ctx):
        return {"structuredContent": {"display_name": "Tokyo"}, "isError": False}

    async def broken(ctx):
        raise RuntimeError("tool failed")

    listed = asyncio.run(middleware(_tool_ctx("203.0.113.1", method="tools/list"), ok))
    assert listed["structuredContent"]["display_name"] == "Tokyo"
    with pytest.raises(RuntimeError):
        asyncio.run(middleware(_tool_ctx("203.0.113.1"), broken))
    asyncio.run(
        middleware(
            _tool_ctx("203.0.113.1"),
            _ready({"isError": True, "structuredContent": {"display_name": "Tokyo"}}),
        )
    )
    smoke = {"user-agent": "claude-code/2.1", "x-umbra-smoke": "1"}
    asyncio.run(middleware(_tool_ctx("203.0.113.1", headers=smoke), ok))
    asyncio.run(middleware(_tool_ctx("203.0.113.1", headers={"user-agent": "UptimeRobot"}), ok))
    assert stats.searches_served == 0


def test_mcp_does_not_count_until_the_recorder_is_accepting(tmp_path):
    stats = _stats(tmp_path)
    install_recorder(stats)
    try:
        middleware = ToolUsageCount()

        async def ok(ctx):
            return {"count": 1}

        asyncio.run(middleware(_tool_ctx("203.0.113.4"), ok))
        assert stats.searches_served == 0
        assert current_recorder() is stats
        stats.enable()
        asyncio.run(middleware(_tool_ctx("203.0.113.4"), ok))
        assert stats.searches_served == 1
    finally:
        install_recorder(None)


def _ready(value):
    async def call_next(ctx):
        return value

    return call_next


# --------------------------------------------------------------------------
# HTTP wiring, when FastAPI is installed
# --------------------------------------------------------------------------


def test_get_stats_is_cached_cors_open_and_rate_limited(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    doc = json.loads((DATA_DIR / "sample_item.json").read_text())
    index_path = tmp_path / "catalog.db"
    with CatalogIndex(index_path) as index:
        for i in range(3):
            item = json.loads(json.dumps(doc))
            item["id"] = f"item-{i}"
            index.add(
                UmbraItem.from_dict(
                    item,
                    href=f"https://example.test/{i}.stac.v2.json",
                )
            )
    app = serve.build_app(index_path)
    with TestClient(app) as client:
        search = client.get("/search", headers={"User-Agent": "Mozilla/5.0"})
        assert search.status_code == 200
        assert client.get("/livez").status_code in {200, 503}
        assert client.get("/.env").status_code == 404
        assert client.get("/search", headers={"X-Umbra-Smoke": "1"}).status_code == 200
        assert client.get("/search", headers={"User-Agent": "UptimeRobot/2.0"}).status_code == 200
        collection = serve.COLLECTION_ID
        items = client.get(
            f"/collections/{collection}/items",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        assert items.status_code == 200
        assert app.state.usage.searches_served == 1
        stats = client.get("/stats", headers={"Origin": "https://umbra-py.space"})
        assert stats.status_code == 200
        assert stats.headers["cache-control"] == "public, max-age=3600"
        assert stats.headers["access-control-allow-origin"] == "*"
        body = stats.json()
        assert body["searches_served"] == 1
        assert body["scenes_indexed"] == 3
        assert body["thumbnails"] == 0
        assert body["index_built_at"] is None
        assert body["top_places"] == []
        assert body["counting_since"].endswith("Z")
        again = client.get("/search", headers={"User-Agent": "Mozilla/5.0"})
        assert again.status_code == 200
        cached = client.get("/stats")
        assert cached.json()["searches_served"] == 1
        assert app.state.usage.searches_served == 2
        spec = client.get("/openapi.json")
    schema = spec.json()
    assert schema["paths"]["/stats"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/UsageStats"}
    assert "UsageStats" in schema["components"]["schemas"]


def test_stats_goes_through_the_rate_limiter(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve

    app = serve.build_app(tmp_path / "catalog.db", rate_limit=1)
    with TestClient(app) as client:
        first = client.get("/stats")
        second = client.get("/stats")
    assert first.status_code == 200
    assert second.status_code == 429
    assert app.state.usage.searches_served == 0


def test_search_still_succeeds_when_recording_raises(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    doc = json.loads((DATA_DIR / "sample_item.json").read_text())
    index_path = tmp_path / "catalog.db"
    with CatalogIndex(index_path) as index:
        index.add(UmbraItem.from_dict(doc, href="https://example.test/a.stac.v2.json"))
    app = serve.build_app(index_path)

    def boom(scope, status):
        raise RuntimeError("stats")

    app.state.usage.observe_http = boom
    with TestClient(app) as client:
        response = client.get("/search")
    assert response.status_code == 200
