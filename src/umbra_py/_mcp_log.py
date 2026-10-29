"""Structured one-line request logs for ``umbra serve``.

Two lines share one logger (:data:`MCP_REQUEST_LOGGER`), which
:func:`umbra_py.serve.serve` routes to stdout as bare JSON for every server —
plain STAC, ``--mcp``, and ``--public``:

* :class:`McpRequestLog` — one line per ``POST /mcp`` (``msg`` is
  ``mcp_request``), peeking at the JSON-RPC body.
* :class:`StacSearchLog` — one line per ``GET`` or ``POST /search`` (``msg``
  is ``stac_search``).

What is logged is metadata. A search logs the client IP, user agent, HTTP
method, path, status and duration, plus the query parameters ``bbox``,
``datetime``, ``limit``, ``collections`` and ``sortby`` when the client sent
them. A ``POST`` body contributes only its top-level key names
(``body_keys``), never the values — a polygon or an id list is the user's
query. Other query parameters, other headers, and the raw body are not logged.

Both are pure ASGI wrappers rather than ``BaseHTTPMiddleware`` so a POST can
read at most :data:`MCP_LOG_BODY_LIMIT` bytes and replay exactly those
messages, after which the downstream app reads the rest of the body from the
socket as if nothing had looked. A logging failure is swallowed: it must
never turn a working request into an error.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import parse_qsl

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
RecordBuilder = Callable[[bytes, bool], dict[str, Any]]

#: The logger :func:`umbra_py.serve.serve` routes to stdout as bare JSON lines.
#: Shared by the MCP and STAC ``/search`` lines; ``msg`` tells them apart.
MCP_REQUEST_LOGGER = "umbra_py.mcp.requests"

#: Bytes of request body read for logging. A JSON-RPC ``initialize`` or
#: ``tools/call``, or a STAC search body, is a few hundred bytes; a larger
#: body is logged without being parsed rather than buffered.
MCP_LOG_BODY_LIMIT = 8192

_MAX_FIELD = 200

#: Query parameters whose *values* may appear on a ``stac_search`` line.
#: Anything else on the query string (an ``intersects`` polygon, an id, a
#: free-text ``area``) stays off the line.
_SEARCH_QUERY_PARAMS = ("bbox", "datetime", "limit", "collections", "sortby")

logger = logging.getLogger(MCP_REQUEST_LOGGER)

#: ``mcp.server.streamable_http.StreamableHTTPServerTransport.terminate``.
#: Stateless mode builds a transport with ``mcp_session_id=None`` and terminates
#: it after every request, so this line is the normal teardown, not a failure.
#: The library logs it at INFO (Railway often surfaces that stderr line as an
#: error). A real session id keeps its level, as do exceptions on the same logger.
_STATELESS_SESSION_TEARDOWN = "Terminating session: None"
_STREAMABLE_HTTP_LOGGER = "mcp.server.streamable_http"


class _StatelessSessionFilter(logging.Filter):
    """Drop ``Terminating session: None`` unless this logger is at DEBUG.

    Relabelling the record is not enough. ``Logger.isEnabledFor`` already
    accepted the original INFO/ERROR line, and a handler left at NOTSET (the
    default, including uvicorn's) emits whatever reaches it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != _STATELESS_SESSION_TEARDOWN:
            return True
        record.levelno = logging.DEBUG
        record.levelname = logging.getLevelName(logging.DEBUG)
        return logging.getLogger(record.name).isEnabledFor(logging.DEBUG)


def quiet_stateless_session_teardown() -> None:
    """Keep a normal stateless MCP teardown off INFO and ERROR handlers.

    Idempotent. Installed for ``umbra serve --mcp`` / ``--public`` and for
    ``umbra mcp --http``, both of which run Streamable HTTP without sessions.
    """
    target = logging.getLogger(_STREAMABLE_HTTP_LOGGER)
    if any(isinstance(existing, _StatelessSessionFilter) for existing in target.filters):
        return
    target.addFilter(_StatelessSessionFilter())


def _clip(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_FIELD else text[:_MAX_FIELD] + "..."


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    return _clip(client[0]) if client else None


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


def _is_search(scope: Scope) -> bool:
    if scope.get("type") != "http" or scope.get("method") not in ("GET", "POST"):
        return False
    return str(scope.get("path", "")).rstrip("/") == "/search"


def _search_query_fields(scope: Scope) -> dict[str, Any]:
    """Allowlisted query values, decoded, in a stable order. Absent keys are omitted."""
    raw = scope.get("query_string") or b""
    text = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
    grouped: dict[str, list[str]] = {}
    for key, value in parse_qsl(text, keep_blank_values=True):
        if key in _SEARCH_QUERY_PARAMS:
            grouped.setdefault(key, []).append(value)
    fields: dict[str, Any] = {}
    for key in _SEARCH_QUERY_PARAMS:
        values = grouped.get(key)
        if not values:
            continue
        clipped = [_clip(value) or "" for value in values]
        fields[key] = clipped[0] if len(clipped) == 1 else clipped
    return fields


def _search_body_fields(body: bytes, truncated: bool) -> dict[str, Any]:
    """Top-level key names of a POST body. Values never leave this function."""
    if truncated:
        return {"body_truncated": True}
    if not body.strip():
        return {"body_keys": []}
    try:
        payload = json.loads(body)
    except ValueError:
        return {"body_unparsed": True}
    if not isinstance(payload, dict):
        return {"body_unparsed": True}
    return {"body_keys": sorted(_clip(key) or "" for key in payload)}


async def _buffer_body(receive: Receive, limit: int) -> tuple[list[Message], bytes, bool]:
    """Read request messages until ``limit`` bytes, without pulling the rest.

    The returned messages are what the caller replays. ``truncated`` is true
    when the client still had body left, so the peeked bytes are not parsed —
    a cut JSON value must not become a log field. One ASGI chunk larger than
    ``limit`` is kept whole (the loop checks the size before the next read),
    matching the original ``POST /mcp`` wrapper.
    """
    buffered: list[Message] = []
    size = 0
    more_body = True
    while more_body and size < limit:
        message = await receive()
        buffered.append(message)
        if message.get("type") != "http.request":
            break
        size += len(message.get("body", b""))
        more_body = bool(message.get("more_body", False))
    body = b"".join(
        message.get("body", b"") for message in buffered if message.get("type") == "http.request"
    )
    return buffered, body, bool(more_body and size >= limit)


def _replayer(buffered: list[Message], receive: Receive) -> Receive:
    async def replay() -> Message:
        if buffered:
            return buffered.pop(0)
        return await receive()

    return replay


async def _observe(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    read_body: bool,
    body_limit: int,
    build_record: RecordBuilder,
) -> None:
    """Call ``app``, then log one JSON line. Logging never fails the request."""
    started = time.perf_counter()
    body = b""
    truncated = False
    downstream_receive = receive
    if read_body:
        buffered, body, truncated = await _buffer_body(receive, body_limit)
        downstream_receive = _replayer(buffered, receive)

    status: int | None = None

    async def capture(message: Message) -> None:
        nonlocal status
        if message.get("type") == "http.response.start":
            status = message.get("status")
        await send(message)

    try:
        await app(scope, downstream_receive, capture)
    except Exception:
        status = status or 500
        raise
    finally:
        try:
            record = build_record(body, truncated)
            record["status"] = status
            record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            logger.info(json.dumps(record, separators=(",", ":")))
        except Exception:
            pass


class McpRequestLog:
    """ASGI wrapper that logs each ``POST .../mcp`` and passes everything through.

    ``GET /mcp`` (the server-to-client SSE stream) and every other method go
    straight to ``app`` without being read or logged. The client IP is the
    ASGI scope's client. ``umbra serve --public`` rewrites that from
    ``X-Real-IP`` (or the rightmost trusted ``X-Forwarded-For`` hop) before
    this mount runs. A plain ``umbra serve`` logs the socket peer.
    ``--proxy-headers`` still lets uvicorn rewrite from the leftmost hop.
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
        await _observe(
            self.app,
            scope,
            receive,
            send,
            read_body=True,
            body_limit=self.body_limit,
            build_record=lambda body, truncated: _mcp_record(scope, body, truncated),
        )


def _mcp_record(scope: Scope, body: bytes, truncated: bool) -> dict[str, Any]:
    record: dict[str, Any] = {
        "level": "info",
        "msg": "mcp_request",
        "client_ip": _client_ip(scope),
        "user_agent": _header(scope, b"user-agent"),
        "mcp_protocol_version": _header(scope, b"mcp-protocol-version"),
    }
    record.update(_body_fields(body, truncated))
    return record


class StacSearchLog:
    """ASGI middleware that logs each ``GET`` or ``POST /search``.

    Mounted on the FastAPI app (outermost user middleware) so the line records
    the status the client actually got, including a rate-limit ``429``, and the
    time spent in the guards in front of the route. ``GET`` is not read — its
    allowlisted parameters are already on the query string. ``POST`` peeks at
    the body the same way :class:`McpRequestLog` does and replays it, so the
    search handler still sees every byte.

    The client IP is the ASGI scope's client. ``umbra serve --public``
    rewrites that from ``X-Real-IP`` (or the rightmost trusted
    ``X-Forwarded-For`` hop) in middleware outside this logger. A plain
    ``umbra serve`` has no proxy, and the socket peer is the client.
    ``--proxy-headers`` still lets uvicorn rewrite from the leftmost hop.
    This trusts the same address the rate limiter does and no other.
    """

    def __init__(self, app: ASGIApp, *, body_limit: int = MCP_LOG_BODY_LIMIT) -> None:
        self.app = app
        self.body_limit = body_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _is_search(scope):
            await self.app(scope, receive, send)
            return
        await _observe(
            self.app,
            scope,
            receive,
            send,
            read_body=scope.get("method") == "POST",
            body_limit=self.body_limit,
            build_record=lambda body, truncated: _search_record(scope, body, truncated),
        )


def _search_record(scope: Scope, body: bytes, truncated: bool) -> dict[str, Any]:
    record: dict[str, Any] = {
        "level": "info",
        "msg": "stac_search",
        "client_ip": _client_ip(scope),
        "user_agent": _header(scope, b"user-agent"),
        "method": scope.get("method"),
        "path": scope.get("path"),
    }
    record.update(_search_query_fields(scope))
    if scope.get("method") == "POST":
        record.update(_search_body_fields(body, truncated))
    return record
