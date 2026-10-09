"""One structured log line per ``POST /mcp`` on ``umbra serve --mcp`` / ``--public``.

The hosted MCP server is stateless, so uvicorn's access log (``POST /mcp 200``)
is the only trace a request leaves: no client name or version, no tool name.
:class:`McpRequestLog` wraps the mounted MCP ASGI app, peeks at the start of the
JSON-RPC body, and logs a single JSON line once the response is done.

What it logs is metadata only. For ``tools/call`` that is the tool name and the
*key names* of its arguments, never their values (a place name or a bbox is the
user's query, not ours to keep). It is a pure ASGI wrapper rather than a
``BaseHTTPMiddleware`` so it can read at most :data:`MCP_LOG_BODY_LIMIT` bytes and
replay exactly those messages to the MCP app, which then reads the rest of the
body from the socket as if nothing had looked. A logging failure is swallowed:
it must never turn a working request into an error.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: The logger :func:`umbra_py.serve.serve` routes to stdout as bare JSON lines.
MCP_REQUEST_LOGGER = "umbra_py.mcp.requests"

#: Bytes of request body read for logging. A JSON-RPC ``initialize`` or
#: ``tools/call`` is a few hundred bytes; a larger body is logged without its
#: method rather than buffered.
MCP_LOG_BODY_LIMIT = 8192

_MAX_FIELD = 200

logger = logging.getLogger(MCP_REQUEST_LOGGER)


def _clip(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_FIELD else text[:_MAX_FIELD] + "..."


def _rpc_fields(message: Any) -> dict[str, Any]:
    """The loggable part of one JSON-RPC message: its method, plus a few params."""
    if not isinstance(message, dict):
        return {"method": None}
    method = message.get("method")
    fields: dict[str, Any] = {"method": _clip(method)}
    params = message.get("params")
    if not isinstance(params, dict):
        return fields
    if method == "initialize":
        info = params.get("clientInfo")
        info = info if isinstance(info, dict) else {}
        fields["client_name"] = _clip(info.get("name"))
        fields["client_version"] = _clip(info.get("version"))
        fields["protocol_version"] = _clip(params.get("protocolVersion"))
    elif method == "tools/call":
        fields["tool"] = _clip(params.get("name"))
        arguments = params.get("arguments")
        fields["argument_keys"] = (
            sorted(_clip(key) or "" for key in arguments) if isinstance(arguments, dict) else []
        )
    return fields


def _body_fields(body: bytes, truncated: bool) -> dict[str, Any]:
    if truncated:
        return {"method": None, "body_truncated": True}
    try:
        payload = json.loads(body)
    except ValueError:
        return {"method": None, "body_unparsed": True}
    if isinstance(payload, list):
        return {"method": "batch", "batch": [_rpc_fields(message) for message in payload]}
    return _rpc_fields(payload)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return _clip(value.decode("latin-1"))
    return None


class McpRequestLog:
    """ASGI wrapper that logs each ``POST .../mcp`` and passes everything through.

    ``GET /mcp`` (the server-to-client SSE stream) and every other method go
    straight to ``app`` without being read or logged. The client IP is the
    ASGI scope's client, which uvicorn already rewrites from ``X-Forwarded-For``
    when it runs with ``--proxy-headers`` (``umbra serve --public``), so this
    trusts the same proxy the rate limiter does and no other.
    """

    def __init__(self, app: ASGIApp, *, body_limit: int = MCP_LOG_BODY_LIMIT) -> None:
        self.app = app
        self.body_limit = body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or not str(scope.get("path", "")).rstrip("/").endswith("/mcp")
        ):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        buffered: list[Message] = []
        size = 0
        more_body = True
        while more_body and size < self.body_limit:
            message = await receive()
            buffered.append(message)
            if message.get("type") != "http.request":
                break
            size += len(message.get("body", b""))
            more_body = bool(message.get("more_body", False))
        body = b"".join(m.get("body", b"") for m in buffered if m.get("type") == "http.request")

        async def replay() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        status: int | None = None

        async def capture(message: Message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = message.get("status")
            await send(message)

        try:
            await self.app(scope, replay, capture)
        except Exception:
            status = status or 500
            raise
        finally:
            self._log(scope, body, more_body and size >= self.body_limit, status, started)

    def _log(
        self, scope: Scope, body: bytes, truncated: bool, status: int | None, started: float
    ) -> None:
        try:
            client = scope.get("client")
            record: dict[str, Any] = {
                "level": "info",
                "msg": "mcp_request",
                "client_ip": _clip(client[0]) if client else None,
                "user_agent": _header(scope, b"user-agent"),
                "mcp_protocol_version": _header(scope, b"mcp-protocol-version"),
            }
            record.update(_body_fields(body, truncated))
            record["status"] = status
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            logger.info(json.dumps(record, separators=(",", ":")))
        except Exception:  # pragma: no cover - logging must never fail a request
            pass
