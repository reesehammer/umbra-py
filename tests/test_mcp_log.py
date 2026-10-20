"""Tests for the per-request JSON log lines (``umbra_py._mcp_log``).

``POST /mcp`` (``msg`` ``mcp_request``) and STAC ``GET``/``POST /search``
(``msg`` ``stac_search``) share one logger.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging

import pytest

from umbra_py._mcp_log import (
    _STREAMABLE_HTTP_LOGGER,
    MCP_LOG_BODY_LIMIT,
    MCP_REQUEST_LOGGER,
    McpRequestLog,
    StacSearchLog,
    _StatelessSessionFilter,
    quiet_stateless_session_teardown,
)


def _scope(
    method: str = "POST",
    path: str = "/mcp",
    headers: dict | None = None,
    query: bytes = b"",
) -> dict:
    headers = {
        "user-agent": "claude-code/2.1",
        "mcp-protocol-version": "2025-06-18",
        "content-type": "application/json",
        **(headers or {}),
    }
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": ("203.0.113.7", 51234),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "query_string": query,
    }


class _EchoApp:
    """Downstream app that reads the whole body, as the MCP SDK does."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.body = b""
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        if scope["method"] == "POST":
            more = True
            while more:
                message = await receive()
                self.body += message.get("body", b"")
                more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _run(app, scope, chunks: list[bytes]) -> list[dict]:
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == MCP_REQUEST_LOGGER]


def _call(caplog, payload, *, status: int = 200, chunks: int = 1, scope=None):
    body = json.dumps(payload).encode()
    step = max(1, len(body) // chunks + 1)
    downstream = _EchoApp(status)
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        _run(
            McpRequestLog(downstream),
            scope or _scope(),
            [body[i : i + step] for i in range(0, len(body), step)],
        )
    assert downstream.body == body
    return _records(caplog)


def test_initialize_logs_client_info_and_protocol(caplog):
    records = _call(
        caplog,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "claude-ai", "version": "0.1.0"},
            },
        },
    )
    assert len(records) == 1
    record = records[0]
    assert record["msg"] == "mcp_request"
    assert record["client_ip"] == "203.0.113.7"
    assert record["user_agent"] == "claude-code/2.1"
    assert record["mcp_protocol_version"] == "2025-06-18"
    assert record["method"] == "initialize"
    assert record["client_name"] == "claude-ai"
    assert record["client_version"] == "0.1.0"
    assert record["protocol_version"] == "2025-06-18"
    assert record["status"] == 200
    assert isinstance(record["duration_ms"], float)


def test_tools_call_logs_argument_keys_never_values(caplog):
    records = _call(
        caplog,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_catalog",
                "arguments": {"place": "Secret Harbour", "limit": 987654, "bbox": [1, 2, 3, 4]},
            },
        },
    )
    (record,) = records
    assert record["method"] == "tools/call"
    assert record["tool"] == "search_catalog"
    assert record["argument_keys"] == ["bbox", "limit", "place"]
    line = caplog.records[-1].getMessage()
    assert "Secret Harbour" not in line
    assert "987654" not in line


def test_other_methods_log_only_the_method(caplog):
    (record,) = _call(caplog, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, status=202)
    assert record["method"] == "tools/list"
    assert record["status"] == 202
    assert "tool" not in record and "client_name" not in record


def test_batch_logs_each_message(caplog):
    (record,) = _call(
        caplog,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_item", "arguments": {"url": "https://secret/x"}},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
    )
    assert record["method"] == "batch"
    assert [m["method"] for m in record["batch"]] == [
        "tools/list",
        "tools/call",
        "notifications/initialized",
    ]
    assert record["batch"][1]["tool"] == "get_item"
    assert record["batch"][1]["argument_keys"] == ["url"]
    assert "secret" not in caplog.records[-1].getMessage()


def test_oversize_body_is_not_parsed_but_reaches_downstream_whole(caplog):
    payload = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "stack_stats", "arguments": {"urls": ["x" * 100] * 200}},
    }
    assert len(json.dumps(payload)) > MCP_LOG_BODY_LIMIT
    (record,) = _call(caplog, payload, chunks=8)
    assert record["method"] is None
    assert record["body_truncated"] is True
    assert record["status"] == 200


def test_multi_chunk_body_under_the_cap_is_parsed(caplog):
    (record,) = _call(
        caplog,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        chunks=4,
    )
    assert record["method"] == "ping"


def test_unparseable_body_still_logs_and_passes_through(caplog):
    downstream = _EchoApp(400)
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        _run(McpRequestLog(downstream), _scope(), [b"not json{"])
    assert downstream.body == b"not json{"
    (record,) = _records(caplog)
    assert record["method"] is None and record["body_unparsed"] is True
    assert record["status"] == 400


@pytest.mark.parametrize(
    "scope",
    [
        _scope(method="GET", headers={"accept": "text/event-stream"}),
        _scope(method="DELETE"),
        _scope(path="/search"),
        {"type": "lifespan"},
    ],
)
def test_get_sse_and_other_traffic_pass_through_unlogged(caplog, scope):
    called = []

    async def receive():  # pragma: no cover - the wrapper must not read these
        raise AssertionError("the wrapper must not read this request")

    async def send(message):  # pragma: no cover
        pass

    async def downstream(scope_, receive_, send_):
        assert receive_ is receive and send_ is send
        called.append(scope_)

    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        asyncio.run(McpRequestLog(downstream)(scope, receive, send))
    assert called == [scope]
    assert _records(caplog) == []


def test_downstream_failure_is_logged_as_500_and_reraised(caplog):
    async def boom(scope, receive, send):
        await receive()
        raise RuntimeError("downstream broke")

    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        with pytest.raises(RuntimeError, match="downstream broke"):
            _run(McpRequestLog(boom), _scope(), [b'{"jsonrpc":"2.0","id":1,"method":"ping"}'])
    (record,) = _records(caplog)
    assert record["method"] == "ping" and record["status"] == 500


def test_a_logging_failure_never_breaks_the_request(caplog, monkeypatch):
    import umbra_py._mcp_log as mod

    def broken(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(mod.logger, "info", broken)
    downstream = _EchoApp()
    sent = _run(McpRequestLog(downstream), _scope(), [b'{"jsonrpc":"2.0","id":1,"method":"ping"}'])
    assert sent[0]["status"] == 200
    assert downstream.body == b'{"jsonrpc":"2.0","id":1,"method":"ping"}'


_HAS_SERVE_MCP = all(importlib.util.find_spec(m) for m in ("fastapi", "mcp", "httpx"))


@pytest.mark.skipif(not _HAS_SERVE_MCP, reason="serve + mcp extras")
def test_build_app_logs_a_real_mcp_initialize(tmp_path, caplog):
    from fastapi.testclient import TestClient

    from umbra_py import serve

    app = serve.build_app(tmp_path / "catalog.db", mcp=True, artifacts=False)
    body = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "claude-ai", "version": "0.1.0"},
        },
    }
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        resp = client.post(
            "/mcp",
            json=body,
            headers={
                "accept": "application/json, text/event-stream",
                "host": "localhost:8000",
                "user-agent": "claude-code/2.1",
                "mcp-protocol-version": "2025-06-18",
            },
        )
    assert resp.status_code == 200
    # The MCP app parsed the replayed body: it answered the initialize request.
    assert '"serverInfo"' in resp.text
    (record,) = _records(caplog)
    assert record["method"] == "initialize"
    assert record["client_name"] == "claude-ai"
    assert record["status"] == 200
    assert record["user_agent"] == "claude-code/2.1"


@pytest.mark.skipif(importlib.util.find_spec("uvicorn") is None, reason="serve extra")
@pytest.mark.parametrize("flags", [{}, {"mcp": True}, {"public": True}])
def test_serve_routes_the_request_logger_to_stdout_as_bare_json(monkeypatch, flags):
    """Plain STAC, ``--mcp`` and ``--public`` all emit the bare-JSON logger.

    ``/search`` exists on every server, so the config is not gated on MCP.
    """
    import uvicorn

    from umbra_py import serve

    seen: dict = {}
    monkeypatch.setattr(serve, "build_app", lambda *a, **kw: object())
    monkeypatch.setattr(serve, "public_secret_names", lambda: [])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    serve.serve(log_level="info", **flags)
    config = seen["log_config"]
    logger = config["loggers"][MCP_REQUEST_LOGGER]
    assert logger == {"handlers": ["mcp_request"], "level": "INFO", "propagate": False}
    assert config["handlers"]["mcp_request"]["stream"] == "ext://sys.stdout"
    assert config["formatters"]["mcp_request"] == {"format": "%(message)s"}
    assert "uvicorn.access" in config["loggers"]


def _search(caplog, scope, chunks, *, status: int = 200):
    downstream = _EchoApp(status)
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        sent = _run(StacSearchLog(downstream), scope, chunks)
    return downstream, sent, _records(caplog)


def test_get_search_logs_allowlisted_query_params_only(caplog):
    scope = _scope(
        method="GET",
        path="/search",
        headers={
            "user-agent": "stac-client/1.2",
            "authorization": "Bearer secret-token",
            "cookie": "session=secret-cookie",
            "x-forwarded-for": "198.51.100.9",
        },
        query=(
            b"bbox=-68.1,10.4,-67.9,10.6"
            b"&datetime=2024-01-01/2024-02-01"
            b"&limit=10"
            b"&collections=umbra-open-data"
            b"&sortby=-datetime"
            b"&intersects=%7B%22secret%22%3A%22hidden-polygon%22%7D"
            b"&area=Classified+Site"
            b"&token=hidden-token"
        ),
    )
    _downstream, sent, records = _search(caplog, scope, [])
    assert sent[0]["status"] == 200
    (record,) = records
    assert record["msg"] == "stac_search"
    assert record["client_ip"] == "203.0.113.7"
    assert record["user_agent"] == "stac-client/1.2"
    assert record["method"] == "GET"
    assert record["path"] == "/search"
    assert record["bbox"] == "-68.1,10.4,-67.9,10.6"
    assert record["datetime"] == "2024-01-01/2024-02-01"
    assert record["limit"] == "10"
    assert record["collections"] == "umbra-open-data"
    assert record["sortby"] == "-datetime"
    assert record["status"] == 200
    assert isinstance(record["duration_ms"], float)
    assert "body_keys" not in record
    line = caplog.records[-1].getMessage()
    assert "hidden-polygon" not in line
    assert "Classified Site" not in line
    assert "hidden-token" not in line
    assert "secret-token" not in line
    assert "secret-cookie" not in line
    assert "198.51.100.9" not in line
    assert "mcp_protocol_version" not in record
    assert "intersects" not in record
    assert "area" not in record
    assert "token" not in record


def test_get_search_does_not_read_the_body(caplog):
    seen: dict = {}

    async def downstream(scope, receive, send):
        seen["receive"] = receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():  # pragma: no cover - GET logging must not pull a body
        raise AssertionError("GET /search logging must not read the body")

    async def send(message):
        seen.setdefault("sent", []).append(message)

    scope = _scope(method="GET", path="/search/", query=b"limit=3")
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        asyncio.run(StacSearchLog(downstream)(scope, receive, send))
    assert seen["receive"] is receive
    assert seen["sent"][0]["status"] == 204
    (record,) = _records(caplog)
    assert record["path"] == "/search/"
    assert record["limit"] == "3"
    assert record["status"] == 204
    assert isinstance(record["duration_ms"], float)


def test_post_search_logs_body_keys_never_values(caplog):
    payload = {
        "bbox": [-68.1, 10.4, -67.9, 10.6],
        "intersects": {"type": "Polygon", "secret": "hidden-polygon"},
        "limit": 987654,
        "collections": ["umbra-open-data"],
        "datetime": "2024-01-01/2024-02-01",
        "ids": ["secret-item-id"],
        "query": {"area": {"like": "Classified Site"}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    body = json.dumps(payload).encode()
    step = max(1, len(body) // 3)
    chunks = [body[i : i + step] for i in range(0, len(body), step)]
    scope = _scope(
        method="POST",
        path="/search",
        query=b"limit=2&bbox=1,2,3,4&area=query-secret",
    )
    downstream, _sent, records = _search(caplog, scope, chunks, status=400)
    assert downstream.body == body
    (record,) = records
    assert record["method"] == "POST"
    assert record["path"] == "/search"
    assert record["status"] == 400
    assert isinstance(record["duration_ms"], float)
    # Query-string allowlist still logs values; the body contributes names only.
    assert record["limit"] == "2"
    assert record["bbox"] == "1,2,3,4"
    assert record["body_keys"] == [
        "bbox",
        "collections",
        "datetime",
        "ids",
        "intersects",
        "limit",
        "query",
        "sortby",
    ]
    line = caplog.records[-1].getMessage()
    assert "hidden-polygon" not in line
    assert "secret-item-id" not in line
    assert "Classified Site" not in line
    assert "query-secret" not in line
    assert "987654" not in line
    assert "properties.datetime" not in line
    assert "-68.1" not in line


def test_post_search_over_the_cap_is_not_parsed_but_replays_the_whole_body(caplog):
    payload = {
        "note": "LEAK-THIS-VALUE",
        "bbox": [-68.1, 10.4, -67.9, 10.6],
        "pad": "y" * (MCP_LOG_BODY_LIMIT * 2),
    }
    body = json.dumps(payload).encode()
    assert len(body) > MCP_LOG_BODY_LIMIT
    # Small chunks, so the peek stops with unread body left (the same rule as
    # POST /mcp: one last chunk may cross the cap, and that is not "truncated"
    # when it also finishes the body).
    chunks = [body[i : i + 512] for i in range(0, len(body), 512)]
    assert len(chunks) > (MCP_LOG_BODY_LIMIT // 512) + 1
    downstream, _sent, records = _search(caplog, _scope(method="POST", path="/search"), chunks)
    assert downstream.body == body
    (record,) = records
    assert record["body_truncated"] is True
    assert "body_keys" not in record
    assert record["status"] == 200
    assert isinstance(record["duration_ms"], float)
    assert "LEAK-THIS-VALUE" not in caplog.records[-1].getMessage()
    assert "-68.1" not in caplog.records[-1].getMessage()


@pytest.mark.parametrize(
    "raw",
    [b'not json {"secret":"hidden-polygon"}', b'["hidden-polygon"]', b"null"],
)
def test_post_search_unparsed_body_does_not_log_the_bytes(caplog, raw):
    downstream, _sent, records = _search(caplog, _scope(method="POST", path="/search"), [raw])
    assert downstream.body == raw
    (record,) = records
    assert record["body_unparsed"] is True
    assert "body_keys" not in record
    assert "hidden-polygon" not in caplog.records[-1].getMessage()


def test_post_search_empty_body_logs_no_keys(caplog):
    downstream, _sent, records = _search(caplog, _scope(method="POST", path="/search"), [b""])
    assert downstream.body == b""
    (record,) = records
    assert record["body_keys"] == []


@pytest.mark.parametrize(
    "scope",
    [
        _scope(method="GET", path="/collections"),
        _scope(method="GET", path="/collections/umbra-open-data/items"),
        _scope(method="POST", path="/items"),
        _scope(method="POST", path="/search/items"),
        _scope(method="POST", path="/mcp"),
        _scope(method="PUT", path="/search"),
        _scope(method="DELETE", path="/search"),
        _scope(method="OPTIONS", path="/search"),
        {"type": "lifespan"},
    ],
)
def test_non_search_traffic_passes_through_unlogged(caplog, scope):
    called = []

    async def receive():  # pragma: no cover - the wrapper must not read these
        raise AssertionError("the wrapper must not read this request")

    async def send(message):  # pragma: no cover
        pass

    async def downstream(scope_, receive_, send_):
        assert receive_ is receive and send_ is send
        called.append(scope_)

    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        asyncio.run(StacSearchLog(downstream)(scope, receive, send))
    assert called == [scope]
    assert _records(caplog) == []


def test_search_downstream_failure_is_logged_as_500_and_reraised(caplog):
    async def boom(scope, receive, send):
        await receive()
        raise RuntimeError("downstream broke")

    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        with pytest.raises(RuntimeError, match="downstream broke"):
            _run(
                StacSearchLog(boom),
                _scope(method="POST", path="/search"),
                [b'{"bbox":[0,0,1,1]}'],
            )
    (record,) = _records(caplog)
    assert record["body_keys"] == ["bbox"]
    assert record["status"] == 500
    line = caplog.records[-1].getMessage()
    assert "0,0,1,1" not in line
    assert "[0, 0, 1, 1]" not in line


def test_search_logging_failure_never_breaks_the_request(caplog, monkeypatch):
    import umbra_py._mcp_log as mod

    def broken(*args, **kwargs):
        raise RuntimeError("log sink down")

    monkeypatch.setattr(mod.logger, "info", broken)
    body = b'{"bbox":[1,2,3,4],"secret":"hidden-polygon"}'
    downstream = _EchoApp()
    sent = _run(StacSearchLog(downstream), _scope(method="POST", path="/search"), [body])
    assert sent[0]["status"] == 200
    assert downstream.body == body
    assert _records(caplog) == []


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="serve extra")
def test_build_app_logs_real_search_and_the_handler_sees_the_body(tmp_path, caplog):
    """The middleware is on the app, and FastAPI still parses the replayed POST."""
    import copy
    from pathlib import Path

    from fastapi.testclient import TestClient

    from umbra_py import serve
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    sample = json.loads((Path(__file__).parent / "data" / "sample_item.json").read_text())
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        doc = copy.deepcopy(sample)
        doc["id"] = "item-0"
        idx.add(
            UmbraItem.from_dict(
                doc,
                href=(
                    "https://example.test/sar-data/tasks/Testville/"
                    "2024-01-01-00-00-00_UMBRA-01/x.stac.v2.json"
                ),
            )
        )
    app = serve.build_app(path, artifacts=False)
    assert app.user_middleware[0].cls is StacSearchLog

    secret = "DoNotLogThisSecret"
    with TestClient(app) as client, caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        listed = client.get(
            "/search",
            params={"limit": "1", "bbox": "-68.1,10.4,-67.9,10.6", "note": secret},
        )
        missed = client.post(
            "/search",
            json={"bbox": [0, 0, 1, 1], "secret_note": secret, "limit": 5},
        )
        health = client.get("/healthz")
    assert listed.status_code == 200
    assert listed.json()["context"]["returned"] == 1
    assert missed.status_code == 200
    # Far from the sample footprint: the bbox in the body was applied.
    assert missed.json()["context"]["returned"] == 0
    assert health.status_code == 200

    records = _records(caplog)
    assert [record["msg"] for record in records] == ["stac_search", "stac_search"]
    get_record, post_record = records
    assert get_record["method"] == "GET"
    assert get_record["path"] == "/search"
    assert get_record["bbox"] == "-68.1,10.4,-67.9,10.6"
    assert get_record["limit"] == "1"
    assert get_record["status"] == 200
    assert isinstance(get_record["duration_ms"], float)
    assert "area" not in get_record
    assert post_record["method"] == "POST"
    assert post_record["body_keys"] == ["bbox", "limit", "secret_note"]
    assert post_record["status"] == 200
    assert isinstance(post_record["duration_ms"], float)
    lines = [rec.getMessage() for rec in caplog.records if rec.name == MCP_REQUEST_LOGGER]
    assert all(secret not in line for line in lines)


@pytest.mark.skipif(not _HAS_SERVE_MCP, reason="serve + mcp extras")
def test_public_app_installs_search_logging_too(tmp_path):
    from umbra_py import serve

    app = serve.build_app(tmp_path / "catalog.db", public=True)
    assert app.user_middleware[0].cls is StacSearchLog
    target = logging.getLogger(_STREAMABLE_HTTP_LOGGER)
    assert any(isinstance(item, _StatelessSessionFilter) for item in target.filters)


def test_stateless_session_teardown_is_logged_at_debug(caplog):
    quiet_stateless_session_teardown()
    quiet_stateless_session_teardown()
    target = logging.getLogger(_STREAMABLE_HTTP_LOGGER)
    assert sum(isinstance(item, _StatelessSessionFilter) for item in target.filters) == 1

    with caplog.at_level(logging.INFO, logger=_STREAMABLE_HTTP_LOGGER):
        target.error("Terminating session: None")
        target.error("Terminating session: abc123")
        target.error("Error handling POST request")
    info_or_above = [record.getMessage() for record in caplog.records]
    assert "Terminating session: None" not in info_or_above
    assert "Terminating session: abc123" in info_or_above
    assert "Error handling POST request" in info_or_above

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_STREAMABLE_HTTP_LOGGER):
        target.error("Terminating session: None")
    debug_lines = [record for record in caplog.records if record.name == _STREAMABLE_HTTP_LOGGER]
    assert len(debug_lines) == 1
    assert debug_lines[0].levelno == logging.DEBUG
    assert debug_lines[0].getMessage() == "Terminating session: None"
