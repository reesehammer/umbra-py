"""One JSON line per MCP tool call (``msg`` ``mcp_tool``).

The line is metadata: a hashed network prefix, argument key names, and a few
coarse values. Place names, URLs, and coordinates stay off it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from types import SimpleNamespace

import pytest

from umbra_py._mcp_log import (
    MCP_REQUEST_LOGGER,
    ToolCallLog,
    _client_network_hash,
    note_tool_error,
    note_tool_result,
)

_SECRET = "Secret Harbour"
_URL = "https://secret.example/a/b?q=1"
_BBOX = [12.345678, -45.5, 12.9, -45.1]


def _records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == MCP_REQUEST_LOGGER]


def _ctx(
    arguments: dict | None,
    *,
    tool: str = "search_catalog",
    host: str | None = "203.0.113.7",
    user_agent: str | None = "claude-code/2.1 (Macintosh; " + _SECRET + ")",
    session_id: str | None = "abcdef0123456789",
    client_info: tuple[str, str] | None = None,
    method: str = "tools/call",
) -> SimpleNamespace:
    headers = {}
    if user_agent is not None:
        headers["user-agent"] = user_agent
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    client = None if host is None else SimpleNamespace(host=host)
    if client_info is None:
        session = SimpleNamespace(client_params=None, _connection=SimpleNamespace(session_id=None))
    else:
        session = SimpleNamespace(
            client_params=SimpleNamespace(
                client_info=SimpleNamespace(name=client_info[0], version=client_info[1])
            ),
            _connection=SimpleNamespace(session_id=None),
        )
    return SimpleNamespace(
        method=method,
        params={"name": tool, "arguments": arguments or {}},
        request=SimpleNamespace(client=client, headers=headers),
        session=session,
    )


def _run(caplog, ctx, call_next) -> list[dict]:
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        asyncio.run(ToolCallLog()(ctx, call_next))
    return _records(caplog)


def test_network_hash_is_the_prefix_not_the_address():
    first = _client_network_hash("203.0.113.7")
    assert first == _client_network_hash("203.0.113.200")
    assert first != _client_network_hash("203.0.114.7")
    assert first == _client_network_hash("::ffff:203.0.113.7")
    same_v6 = _client_network_hash("2001:db8:1:2::1")
    assert same_v6 == _client_network_hash("2001:db8:1:2::ffff")
    assert same_v6 != _client_network_hash("2001:db8:1:3::1")
    assert _client_network_hash("testclient") is None
    assert _client_network_hash(None) is None
    for host in ("203.0.113.7", "203.0.113.0", "::ffff:203.0.113.7", "2001:db8:1:2::1"):
        assert host not in (first or "")
        assert host not in (same_v6 or "")


def test_tool_line_keeps_coarse_values_and_drops_free_text(caplog):
    async def call_next(ctx):
        note_tool_result(
            {
                "count": 2,
                "items": [{"id": "a"}, {"id": "b"}],
                "resolved_place": _SECRET,
                "resolved_bbox": _BBOX,
            }
        )
        return SimpleNamespace(is_error=False)

    arguments = {
        "place": _SECRET,
        "area": "classified site name",
        "bbox": _BBOX,
        "limit": 20,
        "collection": "umbra-open-data",
        "intersects": {"type": "Polygon", "coordinates": [_BBOX]},
        _URL: "nope",
        "limit_note": _SECRET,
    }
    (record,) = _run(caplog, _ctx(arguments), call_next)
    assert record["msg"] == "mcp_tool"
    assert record["level"] == "info"
    assert record["tool"] == "search_catalog"
    assert record["outcome"] == "ok"
    assert record["result_count"] == 2
    assert record["limit"] == 20
    assert record["collection"] == "umbra-open-data"
    assert record["bbox"] is True
    assert record["place"] is True
    assert record["argument_keys"] == [
        "area",
        "bbox",
        "collection",
        "intersects",
        "limit",
        "limit_note",
        "place",
    ]
    assert record["client_name"] == "claude-code"
    assert record["client_version"] == "2.1"
    assert record["session_id"] == "abcdef0123456789"
    assert record["client_net"] == _client_network_hash("203.0.113.7")
    assert isinstance(record["duration_ms"], float)
    line = caplog.records[-1].getMessage()
    assert _SECRET not in line
    assert _URL not in line
    assert "12.345678" not in line
    assert "classified site name" not in line
    assert "203.0.113.7" not in line


def test_error_class_is_logged_and_the_message_is_not(caplog):
    async def call_next(ctx):
        note_tool_error(ValueError(_SECRET))
        return SimpleNamespace(is_error=True)

    (record,) = _run(caplog, _ctx({"place": _SECRET, "limit": "not-a-number"}), call_next)
    assert record["outcome"] == "ValueError"
    assert record["level"] == "error"
    assert record["result_count"] is None
    assert "limit" not in record
    assert record["place"] is True
    assert _SECRET not in caplog.records[-1].getMessage()


def test_validation_failure_without_a_noted_class_is_toolerror(caplog):
    async def call_next(ctx):
        return SimpleNamespace(is_error=True)

    (record,) = _run(caplog, _ctx({"url": _URL}), call_next)
    assert record["outcome"] == "ToolError"
    assert record["argument_keys"] == ["url"]
    assert _URL not in caplog.records[-1].getMessage()


def test_escaped_exception_is_reraised_and_logged(caplog):
    async def call_next(ctx):
        note_tool_error(RuntimeError(_SECRET))
        raise RuntimeError(_SECRET)

    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        with pytest.raises(RuntimeError):
            asyncio.run(ToolCallLog()(_ctx({}), call_next))
    (record,) = _records(caplog)
    assert record["outcome"] == "RuntimeError"
    assert _SECRET not in caplog.records[-1].getMessage()


def test_client_info_wins_over_the_user_agent(caplog):
    async def call_next(ctx):
        note_tool_result({"sites": [1, 2, 3]})
        return SimpleNamespace(is_error=False)

    ctx = _ctx(
        {},
        tool="find_repeat_sites",
        user_agent="other-agent/9.9",
        client_info=("Claude Desktop", "1.2.3"),
        session_id=None,
        host=None,
    )
    ctx.session._connection.session_id = "stdio-session-1"
    (record,) = _run(caplog, ctx, call_next)
    assert record["tool"] == "find_repeat_sites"
    assert record["client_name"] == "Claude Desktop"
    assert record["client_version"] == "1.2.3"
    assert record["session_id"] == "stdio-session-1"
    assert record["client_net"] is None
    assert record["result_count"] == 3
    assert record["bbox"] is False
    assert record["place"] is False


def test_a_url_in_client_info_falls_back_to_the_user_agent(caplog):
    async def call_next(ctx):
        return SimpleNamespace(is_error=False)

    ctx = _ctx({}, user_agent="geod/0.4.1", client_info=(_URL, "0.0.1"), session_id=_URL)
    (record,) = _run(caplog, ctx, call_next)
    assert record["client_name"] == "geod"
    assert record["client_version"] == "0.4.1"
    assert record["session_id"] is None
    assert _URL not in caplog.records[-1].getMessage()


def test_other_methods_are_not_tool_lines(caplog):
    seen = []

    async def call_next(ctx):
        seen.append(ctx.method)
        return "initialize-ok"

    ctx = _ctx({}, method="initialize", client_info=("claude-ai", "0.1.0"))
    with caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER):
        result = asyncio.run(ToolCallLog()(ctx, call_next))
    assert result == "initialize-ok"
    assert seen == ["initialize"]
    assert _records(caplog) == []


def test_sdk_failure_text_is_dropped_and_field_names_stay(caplog):
    from umbra_py._mcp_log import _MCP_SERVER_LOGGER, quiet_tool_failure_text

    quiet_tool_failure_text()
    sdk = logging.getLogger(_MCP_SERVER_LOGGER)
    with caplog.at_level(logging.INFO, logger=_MCP_SERVER_LOGGER):
        sdk.info("Tool 'search_catalog' failed: '%s is not a catalog key'", _SECRET)
        sdk.info("Tool 'search_catalog' rejected arguments: ['place']")
        sdk.info("Tool 'search_catalog' raised an unexpected exception")
    messages = [r.getMessage() for r in caplog.records if r.name == _MCP_SERVER_LOGGER]
    assert all(_SECRET not in message for message in messages)
    assert any("rejected arguments" in message for message in messages)
    assert any("unexpected exception" in message for message in messages)


def test_a_logging_failure_does_not_fail_the_tool(monkeypatch):
    import umbra_py._mcp_log as mod

    def broken(*args, **kwargs):
        raise RuntimeError("sink")

    monkeypatch.setattr(mod.logger, "info", broken)

    async def call_next(ctx):
        return SimpleNamespace(is_error=False)

    result = asyncio.run(ToolCallLog()(_ctx({"limit": 20}), call_next))
    assert result.is_error is False


def test_collections_list_of_tokens_is_kept_and_a_url_is_not(caplog):
    async def call_next(ctx):
        return SimpleNamespace(is_error=False)

    good = _ctx({"collections": ["umbra-open-data", "umbra-cphd"]})
    (record,) = _run(caplog, good, call_next)
    assert record["collection"] == ["umbra-open-data", "umbra-cphd"]

    caplog.clear()
    bad = _ctx({"collections": ["umbra-open-data", _URL]})
    (record,) = _run(caplog, bad, call_next)
    assert "collection" not in record
    assert _URL not in caplog.records[-1].getMessage()


_HAS_SERVE_MCP = all(importlib.util.find_spec(m) for m in ("fastapi", "mcp", "httpx"))


@pytest.mark.skipif(not _HAS_SERVE_MCP, reason="serve + mcp extras")
def test_http_tool_call_logs_outcome_without_arguments(tmp_path, monkeypatch, caplog):
    """The real stateless server path: thread, request, and the tool wrapper."""
    from fastapi.testclient import TestClient

    from umbra_py import mcp_server as ms
    from umbra_py import serve

    state = {"mode": "ok"}

    def fake_search(
        bbox: list[float] | None = None,
        intersects: dict | None = None,
        place: str | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        semantic: bool = False,
        min_score: float = 0.0,
        start: str | None = None,
        end: str | None = None,
        products: list[str] | None = None,
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
        canonical: bool | None = None,
        limit: int = 20,
        max_per_task: int | None = None,
        local: bool | None = None,
    ) -> dict:
        if state["mode"] == "bad":
            raise ValueError(_SECRET + " is not a catalog key")
        return {"count": 2, "items": [{"id": "a"}, {"id": "b"}]}

    fake_search.__name__ = "search_catalog"
    monkeypatch.setattr(ms, "search_catalog", fake_search)
    app = serve.build_app(tmp_path / "catalog.db", mcp=True, artifacts=False)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_catalog",
            "arguments": {
                "place": _SECRET,
                "area": "classified site name",
                "bbox": _BBOX,
                "limit": 20,
            },
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "host": "localhost:8000",
        "user-agent": "geod/0.4.1",
        "mcp-session-id": "abcdef0123456789",
        "mcp-protocol-version": "2025-06-18",
    }
    with (
        TestClient(app, client=("203.0.113.9", 50000)) as client,
        caplog.at_level(logging.INFO, logger=MCP_REQUEST_LOGGER),
    ):
        ok = client.post("/mcp", json=body, headers=headers)
        assert ok.status_code == 200
        assert _SECRET not in ok.text
        assert "12.345678" not in ok.text
        state["mode"] = "bad"
        bad = client.post("/mcp", json=body, headers=headers)
        assert bad.status_code == 200

    tools = [r for r in _records(caplog) if r["msg"] == "mcp_tool"]
    assert [r["outcome"] for r in tools] == ["ok", "ValueError"]
    assert tools[0]["tool"] == "search_catalog"
    assert tools[0]["result_count"] == 2
    assert tools[0]["limit"] == 20
    assert tools[0]["bbox"] is True
    assert tools[0]["place"] is True
    assert tools[0]["client_name"] == "geod"
    assert tools[0]["client_version"] == "0.4.1"
    assert tools[0]["session_id"] == "abcdef0123456789"
    assert tools[0]["client_net"] == _client_network_hash("203.0.113.9")
    assert tools[0]["client_net"] == _client_network_hash("203.0.113.7")
    requests = [r for r in _records(caplog) if r["msg"] == "mcp_request"]
    assert requests
    assert all(r["client_net"] == tools[0]["client_net"] for r in requests)
    blob = "\n".join(r.getMessage() for r in caplog.records if r.name == MCP_REQUEST_LOGGER)
    assert _SECRET not in blob
    assert "classified site name" not in blob
    assert "12.345678" not in blob
    assert "203.0.113.9" not in blob
    # The client still receives the result and the error text. The text is
    # JSON-encoded inside the SSE payload, so the count is escaped.
    assert "isError" in ok.text and "false" in ok.text
    assert _SECRET in bad.text
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert _SECRET not in everything
    assert "classified site name" not in everything


@pytest.mark.skipif(not _HAS_SERVE_MCP, reason="serve + mcp extras")
def test_server_installs_the_tool_log_without_changing_the_contract():
    from umbra_py import mcp_server as ms

    server = ms.build_server()
    assert any(isinstance(item, ToolCallLog) for item in server.middleware)
    tools = asyncio.run(server.list_tools())
    search = next(tool for tool in tools if tool.name == "search_catalog")
    assert search.input_schema["properties"]["limit"]["default"] == 20
