"""Small shared HTTP helpers built on :mod:`requests`.

Umbra's open data is served over plain, anonymous HTTPS (both the STAC catalog
JSON and the data assets), so we never need AWS credentials or signed requests.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from collections.abc import Callable, Collection
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import HTTPError as Urllib3HTTPError
from urllib3.util.retry import Retry

from . import __version__

DEFAULT_TIMEOUT = 30

#: Public-mode JSON body cap. A STAC item is much smaller; past this the read
#: stops and raises rather than buffering the rest.
SAFE_MAX_BYTES = 2 * 1024 * 1024
#: Wall-clock budget for one public-mode JSON fetch, connect included.
#: Enforced across reads (see :func:`safe_get`), not reset on each chunk.
SAFE_DEADLINE_S = 10.0


class _PublicJsonAllowlist:
    """Process-wide allowlist for :func:`get_json` during a public-host fetch.

    A :class:`contextvars.ContextVar` does not reach the threads
    ``ThreadPoolExecutor`` uses for sidecar reads on this interpreter, so the
    flag is a depth count those workers can see. Library and CLI callers
    leave the depth at zero and keep the retrying GET.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0
        self._allow: Callable[[str], bool] | None = None

    def push(self, allow: Callable[[str], bool]) -> None:
        with self._lock:
            self._depth += 1
            self._allow = allow

    def pop(self) -> None:
        with self._lock:
            self._depth -= 1
            if self._depth <= 0:
                self._depth = 0
                self._allow = None

    def current(self) -> Callable[[str], bool] | None:
        with self._lock:
            return self._allow


_public_json_allowlist = _PublicJsonAllowlist()

#: How a model POST (Anthropic, or an OpenAI-compatible gateway like OpenRouter)
#: fails transiently. The first two are ordinary HTTP; the third is the one a
#: plain status check misses -- an HTTP **200** whose *body* carries an error
#: object instead of a completion. OpenRouter returns that when an upstream
#: provider errors mid-request, so a single narration can fail on a blip the very
#: next call rides out. A 4xx that is not one of these (400 bad request, 401 bad
#: key, 402 out of credit) is a real error and is raised without retrying.
_MODEL_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: Attempts and base backoff (seconds, multiplied by the attempt number) for a
#: model POST. Three attempts with 1.5s/3s waits rides out a brief provider blip
#: without turning a permanent error into a long stall.
_MODEL_ATTEMPTS = 3
_MODEL_BACKOFF_S = 1.5

# A single transient S3 hiccup (a 503, a dropped connection) shouldn't fail an
# entire multi-minute index build or a large download. Retry idempotent GETs a
# few times with exponential backoff on the status codes S3 uses for throttling
# and transient faults. Mounted on the shared session, so every caller
# (catalog walk, sidecar fetch, geocode, download) inherits it.
_RETRY = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "HEAD"),
    raise_on_status=False,
)


# The session is shared across a small thread pool (the catalog walk fetches an
# acquisition's sidecars concurrently -- see ``UmbraCatalog._items_from_sidecars``),
# so the connection pool has to hold more than urllib3's default of 10 to avoid
# discarding and re-opening connections under that fan-out. This bound comfortably
# covers the sidecar worker count with headroom.
_POOL_SIZE = 16


def default_session() -> requests.Session:
    """Return a :class:`requests.Session` with a descriptive user agent and
    retry/backoff on transient HTTP failures.

    The session is safe to share across a small thread pool: its connection pool
    is sized (:data:`_POOL_SIZE`) to hold the concurrent sidecar fetches the
    catalog walk issues without churning connections.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": f"umbra-py/{__version__}"})
    adapter = HTTPAdapter(
        max_retries=_RETRY,
        pool_connections=_POOL_SIZE,
        pool_maxsize=_POOL_SIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _hostname(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def _url_is_allowed(url: str, allowed_hosts: Collection[str] | Callable[[str], bool]) -> bool:
    """Whether ``url`` may be fetched.

    A callable is a full-URL predicate (the public bucket check, which also
    requires the bucket name on a path-style host). A collection is a set of
    https hostnames: port 443 or default, no userinfo.
    """
    if callable(allowed_hosts):
        return bool(allowed_hosts(url))
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443) or not host:
        return False
    normalized = host.lower().rstrip(".")
    return normalized in {item.lower().rstrip(".") for item in allowed_hosts}


def _address_is_blocked(text: str) -> bool:
    """Private, loopback, link-local, or unspecified. IPv4-mapped addresses count."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def _host_has_blocked_address(host: str) -> bool:
    """Whether ``host`` resolves to an address :func:`_address_is_blocked` rejects.

    A resolver failure returns False. The GET then fails the same way it would
    have without this check, so an offline test double does not become a refusal.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        if _address_is_blocked(str(sockaddr[0])):
            return True
    return False


def _response_socket(resp: requests.Response) -> socket.socket | None:
    """The live socket under a streamed ``requests`` response, if there is one.

    urllib3 applies the read timeout once, before the status line. A body that
    drips a byte just inside that window would otherwise never hit a total
    deadline. Tightening this socket before each read is what makes
    ``total_deadline`` cover the whole transfer.
    """
    # http.client HTTPResponse → BufferedReader → SocketIO. A mocked body
    # has none of these, and then the read simply returns.
    fp = getattr(resp.raw, "_fp", None)
    reader = getattr(fp, "fp", None)
    raw = getattr(reader, "raw", None)
    sock = getattr(raw, "_sock", None)
    if isinstance(sock, socket.socket):
        return sock
    return None


def _read_bounded(
    resp: requests.Response,
    *,
    url: str,
    max_bytes: int,
    deadline: float,
    budget: float,
) -> bytes:
    """Read ``resp`` until EOF, ``max_bytes``, or ``deadline`` (monotonic)."""
    sock = _response_socket(resp)
    chunks: list[bytes] = []
    total = 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ValueError(f"Refusing to fetch {url!r}: exceeded the {budget:g}s deadline.")
        if sock is not None:
            try:
                # One successful recv restarts this timer, so each call is a
                # single read1 and the next iteration shrinks it to what is
                # left of the original deadline.
                sock.settimeout(left)
            except OSError:
                sock = None
        try:
            chunk = resp.raw.read1(64 * 1024)
        except (requests.RequestException, TimeoutError, Urllib3HTTPError) as exc:
            # The socket timeout was set to the time still left, so a stall
            # lands on the deadline. A reset well before that is a real error.
            if time.monotonic() + 0.05 >= deadline or "timed out" in str(exc).lower():
                raise ValueError(
                    f"Refusing to fetch {url!r}: exceeded the {budget:g}s deadline."
                ) from exc
            raise
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Refusing to fetch {url!r}: response exceeds {max_bytes} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def safe_get(
    url: str,
    allowed_hosts: Collection[str] | Callable[[str], bool],
    *,
    max_bytes: int = SAFE_MAX_BYTES,
    total_deadline: float = SAFE_DEADLINE_S,
) -> bytes:
    """GET ``url`` with an allowlist, no redirects, no retries, and a hard cap.

    ``allowed_hosts`` is a set of https hostnames or a predicate on the whole
    URL. The public item fetch passes the open-data bucket check, which also
    rejects a path-style URL whose first segment is not that bucket.

    The body is streamed. More than ``max_bytes`` raises :class:`ValueError`.
    ``total_deadline`` is a wall-clock budget across the connect and every
    read, not a timeout that starts over on each chunk. A host that resolves
    to a private, loopback, link-local, or unspecified address is refused
    before the connection. Redirects are not followed and not requested.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("Refusing to fetch an empty URL.")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive.")
    if total_deadline <= 0:
        raise ValueError("total_deadline must be positive.")
    if not _url_is_allowed(url, allowed_hosts):
        raise ValueError(f"Refusing to fetch {url!r}: the host is not allowed.")
    host = _hostname(url)
    if host and _host_has_blocked_address(host):
        raise ValueError(
            f"Refusing to fetch {url!r}: the host resolves to a private, "
            "loopback, or link-local address."
        )

    deadline = time.monotonic() + total_deadline
    connect_timeout = min(total_deadline, 3.0)
    session = requests.Session()
    try:
        session.headers.update({"User-Agent": f"umbra-py/{__version__}"})
        adapter = HTTPAdapter(max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        try:
            resp = session.get(
                url,
                stream=True,
                allow_redirects=False,
                timeout=(connect_timeout, total_deadline),
            )
        except requests.Timeout as exc:
            raise ValueError(
                f"Refusing to fetch {url!r}: exceeded the {total_deadline:g}s deadline."
            ) from exc
        try:
            if 300 <= resp.status_code < 400:
                raise ValueError(
                    f"Refusing to fetch {url!r}: the server returned HTTP "
                    f"{resp.status_code} and redirects are not followed."
                )
            resp.raise_for_status()
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    advertised = int(str(declared).strip())
                except ValueError:
                    advertised = None
                if advertised is not None and advertised > max_bytes:
                    raise ValueError(
                        f"Refusing to fetch {url!r}: response exceeds {max_bytes} bytes."
                    )
            return _read_bounded(
                resp, url=url, max_bytes=max_bytes, deadline=deadline, budget=total_deadline
            )
        finally:
            resp.close()
    finally:
        session.close()


def get_json(url: str, session: requests.Session | None = None, **kwargs: Any) -> dict:
    """Fetch and decode a JSON document.

    While a public-host MCP network section has pushed
    :data:`_public_json_allowlist`, this uses :func:`safe_get` instead of the
    shared retrying session. Library and CLI callers leave that unset and
    keep the retrying GET. Worker threads see the same allowlist.
    """
    allow = _public_json_allowlist.current()
    if allow is not None:
        raw = safe_get(url, allow)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Response from {url!r} was not JSON.") from exc
        if not isinstance(body, dict):
            raise ValueError(f"Response from {url!r} was not a JSON object.")
        return body
    sess = session or default_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    resp = sess.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _provider_error_message(body: Any) -> str | None:
    """The human-readable message from a model endpoint's error body, or ``None``.

    Covers the OpenAI/OpenRouter shape (``{"error": {"message": ...}}`` or a bare
    ``{"error": "..."}``) and the Anthropic one (``{"type": "error", "error":
    {"message": ...}}``). Returns ``None`` for a normal completion, so a caller
    can tell "this 200 is actually an error" from "this 200 is an answer"."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if err is None:
        return None
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        message = err.get("message")
        return str(message) if message else json.dumps(err)[:300]
    return str(err)


def post_model_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    *,
    error_cls: type[Exception],
    timeout: int = 120,
    attempts: int = _MODEL_ATTEMPTS,
) -> dict[str, Any]:
    """POST JSON to a model endpoint, retrying transient failures, and return the
    decoded body.

    Retries a dropped connection, an HTTP 429/5xx, and -- the case a plain status
    check misses -- an HTTP 200 whose body is an error object rather than a
    completion (see :data:`_MODEL_RETRY_STATUS`). Between attempts it backs off
    (:data:`_MODEL_BACKOFF_S`). On the final failure, or immediately for a
    non-transient error (a 400/401/402), it raises ``error_cls`` with the
    surfaced message -- the provider's own words when it gave any, so a caller
    (and a build log) learns *why* rather than seeing a bare "unexpected shape".

    ``error_cls`` is the module's own exception (``DescribeError`` / ``AskError``),
    passed in to keep this shared helper free of a dependency on either.
    """
    last_msg = "no response"
    for attempt in range(1, attempts + 1):
        transient = False
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_msg, transient = f"request failed: {exc}", True
        else:
            if resp.status_code >= 400:
                last_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                transient = resp.status_code in _MODEL_RETRY_STATUS
            else:
                try:
                    body = resp.json()
                except ValueError as exc:
                    last_msg, transient = f"non-JSON response: {exc}", True
                else:
                    message = _provider_error_message(body)
                    if message is None:
                        return body
                    # A 200 carrying an error body: surface it and retry, since it
                    # is usually a mid-request upstream blip rather than a verdict.
                    last_msg, transient = f"endpoint returned an error: {message}", True
        if not transient or attempt == attempts:
            raise error_cls(f"The model endpoint returned an error ({last_msg}).")
        time.sleep(_MODEL_BACKOFF_S * attempt)
    raise error_cls(f"The model endpoint returned an error ({last_msg}).")  # pragma: no cover
