"""Tests for the per-request ``POST /mcp`` log line (``umbra_py._mcp_log``)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging

import pytest

from umbra_py._mcp_log import MCP_LOG_BODY_LIMIT, MCP_REQUEST_LOGGER, McpRequestLog


def _scope(method: str = "POST", path: str = "/mcp", headers: dict | None = None) -> dict:
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
@pytest.mark.parametrize(("flags", "wired"), [({"public": True}, True), ({}, False)])
def test_serve_routes_the_mcp_logger_to_stdout_as_bare_json(monkeypatch, flags, wired):
    import uvicorn

    from umbra_py import serve

    seen: dict = {}
    monkeypatch.setattr(serve, "build_app", lambda *a, **kw: object())
    monkeypatch.setattr(serve, "public_secret_names", lambda: [])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    serve.serve(log_level="info", **flags)
    if not wired:
        assert "log_config" not in seen
        return
    config = seen["log_config"]
    logger = config["loggers"][MCP_REQUEST_LOGGER]
    assert logger == {"handlers": ["mcp_request"], "level": "INFO", "propagate": False}
    assert config["handlers"]["mcp_request"]["stream"] == "ext://sys.stdout"
    assert config["formatters"]["mcp_request"] == {"format": "%(message)s"}
    assert "uvicorn.access" in config["loggers"]
