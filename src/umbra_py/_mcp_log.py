"""Structured one-line logs for MCP and for ``umbra serve``.

Three lines share one logger (:data:`MCP_REQUEST_LOGGER`), which
:func:`umbra_py.serve.serve` routes to stdout as bare JSON. ``umbra mcp``
(stdio or ``--http``) emits the tool line on the same logger; stdio does not
configure the handler, so it follows the process logger (stderr, not the
JSON-RPC pipe).

* :class:`McpRequestLog` — one line per ``POST /mcp`` (``msg`` is
  ``mcp_request``), peeking at the JSON-RPC body.
* :class:`StacSearchLog` — one line per ``GET`` or ``POST /search`` (``msg``
  is ``stac_search``).
* :class:`ToolCallLog` — one line per MCP ``tools/call`` (``msg`` is
  ``mcp_tool``). Server middleware, not an ASGI wrapper, so it sees whether
  the tool succeeded.

What is logged is metadata. The HTTP lines record ``client_ip``, the peer the
transport already resolved (the ASGI scope's client). uvicorn rewrites that
from ``X-Forwarded-For`` when it runs with ``--proxy-headers`` (``umbra serve
--public``). The tool line does not record the address. Its network field is
:func:`_client_network_hash`: the first 16 hex characters of SHA-256 over
``24:<IPv4 /24>`` or ``64:<IPv6 /64>``. An IPv4-mapped IPv6 address uses the
IPv4 ``/24``. The same prefix hashes the same on every replica. The peer is
the request the MCP SDK attached. This module does not read
``X-Forwarded-For`` or ``X-Real-IP``; a middleware that rewrites that peer
before the log runs is the address the hash is taken from.

A search logs the client IP, the user agent, HTTP method, path, status and
duration, plus the query parameters ``bbox``, ``datetime``, ``limit``,
``collections`` and ``sortby`` when the client sent them. A ``POST`` body
contributes only its top-level key names (``body_keys``), never the values.
A tool call logs the tool name, duration, ``ok`` or the error class, a
result count when the reply already carries one, client name and version,
session id, argument key names, ``limit`` when it is an integer, a short
``collection`` token, whether ``bbox`` or ``place`` was set, and the network
hash. Free text and URLs are not logged.

The HTTP lines are ASGI wrappers rather than ``BaseHTTPMiddleware`` so a POST
can read at most :data:`MCP_LOG_BODY_LIMIT` bytes and replay exactly those
messages, after which the downstream app reads the rest of the body from the
socket as if nothing had looked. A logging failure is swallowed: it must
never turn a working request into an error.
"""

from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import json
import logging
import re
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


#: ``mcp.server.mcpserver.server`` logs ``Tool 'name' failed: '<text>'`` at INFO
#: for an anticipated tool error (``logger.info("Tool %r failed: %r", name,
#: str(exc))``). The text is the exception message: the caller's place name or
#: URL. The line stays, with that text replaced by ``<redacted>``. The tool
#: name stays, as does an exception class when the message includes one
#: (``ValueError('<redacted>')``). ``Tool 'name' rejected arguments`` (field
#: names only) and unexpected-exception tracebacks are different messages and
#: stay as logged.
_TOOL_REPR = r"(?:'(?:\\'|[^'])*'|\"(?:\\\"|[^\"])*\")"
_SDK_TOOL_FAILURE = re.compile(
    rf"^Tool (?P<name>{_TOOL_REPR}) failed: (?P<detail>.*)\Z",
    re.DOTALL,
)
_EXC_WRAP = re.compile(r"(?P<exc>[A-Za-z_][A-Za-z0-9_]*)\((?P<inner>.*)\)\Z", re.DOTALL)
_REDACTED = "<redacted>"
_MCP_SERVER_LOGGER = "mcp.server.mcpserver.server"


def _redact_quoted(text: str) -> str:
    """Replace the body of one quoted string. An unquoted tail becomes ``'<redacted>'``."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return f"{text[0]}{_REDACTED}{text[-1]}"
    return f"'{_REDACTED}'"


def _redact_tool_failure_message(message: str) -> str | None:
    """The SDK failure line with the user text removed, or ``None`` if it is not one."""
    match = _SDK_TOOL_FAILURE.match(message)
    if match is None:
        return None
    detail = match.group("detail")
    wrapped = _EXC_WRAP.match(detail)
    if wrapped is not None:
        detail = f"{wrapped.group('exc')}({_redact_quoted(wrapped.group('inner'))})"
    else:
        detail = _redact_quoted(detail)
    return f"Tool {match.group('name')} failed: {detail}"


class _ToolFailureTextFilter(logging.Filter):
    """Replace the quoted text in ``Tool '<name>' failed: ...`` with ``<redacted>``.

    The record stays, including the tool name and an exception class when the
    message has one. Other records are unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = _redact_tool_failure_message(message)
        if redacted is None:
            return True
        record.msg = redacted
        record.args = ()
        if "message" in record.__dict__:
            record.message = redacted
        return True


def quiet_tool_failure_text() -> None:
    """Redact anticipated tool-error text on the MCP server logger.

    The SDK line stays, so the tool name is still visible. Idempotent.
    Installed when the server is built, for stdio and HTTP.
    """
    target = logging.getLogger(_MCP_SERVER_LOGGER)
    if any(isinstance(existing, _ToolFailureTextFilter) for existing in target.filters):
        return
    target.addFilter(_ToolFailureTextFilter())


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


_ARG_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_COLLECTION = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CLIENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}\Z")
_CLIENT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}\Z")
_UA_PRODUCT = re.compile(r"([A-Za-z][A-Za-z0-9._+-]{0,63})/([A-Za-z0-9][A-Za-z0-9._+-]{0,31})")
_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_COUNT_LIST_KEYS = ("items", "sites", "matches", "passes", "results", "features")
_LIMIT_MAX = 10_000_000
_COLLECTION_MAX = 8


def _client_ip(scope: Scope) -> str | None:
    client = scope.get("client")
    return _clip(client[0]) if client else None


def _client_network_hash(host: str | None) -> str | None:
    """Hash of the peer's IPv4 ``/24`` or IPv6 ``/64``, or ``None`` if it is not an IP.

    The digest is SHA-256 of ``"{prefix}:{network address}"`` truncated to 16
    hex characters. The address and the prefix text are not returned.
    """
    if not isinstance(host, str) or not host:
        return None
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    prefix = 24 if isinstance(ip, ipaddress.IPv4Address) else 64
    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    material = f"{prefix}:{network.network_address}"
    return hashlib.sha256(material.encode("ascii")).hexdigest()[:16]


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


class _ToolNote:
    """Mutable scratch the tool thread shares with the middleware task.

    :mod:`anyio` copies the context into the worker thread, so the thread and
    the task hold the same object. Assigning attributes is visible across that
    copy; replacing the context variable is not.
    """

    __slots__ = ("error_class", "result_count")

    def __init__(self) -> None:
        self.error_class: str | None = None
        self.result_count: int | None = None


_tool_note: contextvars.ContextVar[_ToolNote | None] = contextvars.ContextVar(
    "umbra_mcp_tool_note", default=None
)


def note_tool_error(exc: BaseException) -> None:
    """Remember the exception class for the tool call in progress."""
    try:
        note = _tool_note.get()
        if note is not None and note.error_class is None:
            note.error_class = _class_name(exc)
    except Exception:
        return


def note_tool_result(result: Any) -> None:
    """Remember a cheap result count for the tool call in progress."""
    try:
        note = _tool_note.get()
        if note is not None and note.result_count is None:
            note.result_count = _cheap_result_count(result)
    except Exception:
        return


def _class_name(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _ERROR_CLASS.fullmatch(name) else "error"


def _cheap_result_count(result: Any) -> int | None:
    """A count the result already carries. Does not walk the payload."""
    if not isinstance(result, dict):
        return None
    count = result.get("count")
    if type(count) is int and count >= 0:
        return count
    for key in _COUNT_LIST_KEYS:
        value = result.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _argument_mapping(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    dump = getattr(arguments, "model_dump", None)
    if not callable(dump):
        return {}
    try:
        dumped = dump()
    except Exception:
        return {}
    return dumped if isinstance(dumped, dict) else {}


def _collection_token(value: Any) -> str | None:
    if isinstance(value, str) and _COLLECTION.fullmatch(value):
        return value
    return None


def _collection_value(arguments: dict[str, Any]) -> str | list[str] | None:
    if "collection" in arguments:
        token = _collection_token(arguments["collection"])
        if token is not None:
            return token
    value = arguments.get("collections")
    if not isinstance(value, list) or not value or len(value) > _COLLECTION_MAX:
        return None
    tokens: list[str] = []
    for item in value:
        token = _collection_token(item)
        if token is None:
            return None
        tokens.append(token)
    return tokens


def _limit_value(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= _LIMIT_MAX:
        return value
    return None


def _was_set(value: Any) -> bool:
    return value not in (None, False, "", [], {})


def _label(value: Any, pattern: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if pattern.fullmatch(text) else None


def _user_agent_product(header: str | None) -> tuple[str | None, str | None]:
    if not isinstance(header, str) or not header:
        return None, None
    match = _UA_PRODUCT.search(header[:200])
    if match is None:
        return None, None
    return match.group(1), match.group(2)


def _header_value(request: Any, name: str) -> str | None:
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        value = getter(name)
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _request_host(request: Any) -> str | None:
    client = getattr(request, "client", None)
    if client is None:
        return None
    host = client[0] if isinstance(client, tuple) else getattr(client, "host", None)
    if isinstance(host, bytes):
        host = host.decode("latin-1", "replace")
    return host if isinstance(host, str) else None


def _session_id(ctx: Any, request: Any) -> str | None:
    header = _label(_header_value(request, "mcp-session-id"), _SESSION_ID)
    if header is not None:
        return header
    session = getattr(ctx, "session", None)
    connection = getattr(session, "_connection", None)
    if connection is None:
        connection = getattr(session, "connection", None)
    return _label(getattr(connection, "session_id", None), _SESSION_ID)


def _initialize_client(ctx: Any) -> tuple[str | None, str | None]:
    session = getattr(ctx, "session", None)
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    if info is None and isinstance(params, dict):
        info = params.get("clientInfo") or params.get("client_info")
    if isinstance(info, dict):
        name, version = info.get("name"), info.get("version")
    else:
        name, version = getattr(info, "name", None), getattr(info, "version", None)
    safe_name = _label(name, _CLIENT_NAME)
    if safe_name is None:
        return None, None
    return safe_name, _label(version, _CLIENT_VERSION)


def _client_identity(ctx: Any, request: Any) -> tuple[str | None, str | None]:
    name, version = _initialize_client(ctx)
    if name is not None:
        return name, version
    return _user_agent_product(_header_value(request, "user-agent"))


def _params_name_and_args(ctx: Any) -> tuple[Any, Any]:
    params = getattr(ctx, "params", None)
    if isinstance(params, dict):
        return params.get("name"), params.get("arguments")
    return getattr(params, "name", None), getattr(params, "arguments", None)


def _result_is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError") or result.get("is_error"))
    return bool(getattr(result, "is_error", False))


def _outcome(note: _ToolNote, escaped: BaseException | None, result: Any) -> str:
    if escaped is not None:
        return note.error_class or _class_name(escaped)
    if _result_is_error(result):
        return note.error_class or "ToolError"
    return "ok"


def _emit_tool_line(
    ctx: Any, note: _ToolNote, escaped: BaseException | None, started: float, result: Any
) -> None:
    request = getattr(ctx, "request", None)
    name, arguments = _params_name_and_args(ctx)
    mapping = _argument_mapping(arguments)
    client_name, client_version = _client_identity(ctx, request)
    outcome = _outcome(note, escaped, result)
    record: dict[str, Any] = {
        "level": "error" if outcome != "ok" else "info",
        "msg": "mcp_tool",
        "tool": name if isinstance(name, str) and _ARG_KEY.fullmatch(name) else "unknown",
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        "outcome": outcome,
        "result_count": note.result_count if outcome == "ok" else None,
        "client_name": client_name,
        "client_version": client_version,
        "session_id": _session_id(ctx, request),
        "client_net": _client_network_hash(_request_host(request)),
        "argument_keys": sorted(
            key for key in mapping if isinstance(key, str) and _ARG_KEY.fullmatch(key)
        ),
        "bbox": _was_set(mapping.get("bbox")),
        "place": _was_set(mapping.get("place")),
    }
    if "limit" in mapping:
        limit = _limit_value(mapping["limit"])
        if limit is not None:
            record["limit"] = limit
    collection = _collection_value(mapping)
    if collection is not None:
        record["collection"] = collection
    line = json.dumps(record, separators=(",", ":"))
    if outcome == "ok":
        logger.info(line)
    else:
        logger.error(line)


class ToolCallLog:
    """One JSON line per ``tools/call``. Every other method passes through.

    Installed on the MCP server, so stdio and Streamable HTTP both emit it.
    The tool wrapper records the original exception class and a cheap result
    count on :data:`_tool_note`; this middleware writes the line after the
    call. It does not change the result or the error the client sees.
    """

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if getattr(ctx, "method", None) != "tools/call":
            return await call_next(ctx)
        note = _ToolNote()
        token = _tool_note.set(note)
        started = time.perf_counter()
        escaped: BaseException | None = None
        result: Any = None
        try:
            result = await call_next(ctx)
            return result
        except BaseException as exc:
            escaped = exc
            raise
        finally:
            try:
                _emit_tool_line(ctx, note, escaped, started, result)
            except Exception:
                pass
            _tool_note.reset(token)
