"""Small shared HTTP helpers built on :mod:`requests`.

Umbra's open data is served over plain, anonymous HTTPS (both the STAC catalog
JSON and the data assets), so we never need AWS credentials or signed requests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

DEFAULT_TIMEOUT = 30

#: Public-mode JSON body cap. A STAC item is much smaller; past this the read
#: stops and raises rather than buffering the rest.
SAFE_MAX_BYTES = 2 * 1024 * 1024
#: Wall-clock budget for one public-mode JSON fetch, connect included.
SAFE_DEADLINE_S = 10.0
_SAFE_CHUNK = 65536

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


def _read_bounded(
    resp: requests.Response,
    *,
    url: str,
    max_bytes: int,
    deadline: float,
    budget: float,
) -> bytes:
    """Read ``resp`` until EOF, ``max_bytes``, or ``deadline`` (monotonic).

    Chunks come from ``iter_content`` so Content-Encoding is decoded and the
    read stays on the public ``requests`` surface. The request's own timeout
    is ``budget``; this loop also refuses a chunk that arrives after it.
    """
    chunks: list[bytes] = []
    total = 0
    iterator = resp.iter_content(chunk_size=_SAFE_CHUNK)
    while True:
        if time.monotonic() >= deadline:
            raise ValueError(f"Refusing to fetch {url!r}: exceeded the {budget:g}s deadline.")
        try:
            chunk = next(iterator)
        except StopIteration:
            break
        except (requests.RequestException, TimeoutError) as exc:
            if time.monotonic() + 0.05 >= deadline or "timed out" in str(exc).lower():
                raise ValueError(
                    f"Refusing to fetch {url!r}: exceeded the {budget:g}s deadline."
                ) from exc
            raise
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"Refusing to fetch {url!r}: response exceeds {max_bytes} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def safe_get(
    url: str,
    allowed: Callable[[str], bool],
    *,
    max_bytes: int = SAFE_MAX_BYTES,
    total_deadline: float = SAFE_DEADLINE_S,
) -> bytes:
    """GET ``url`` when ``allowed`` says so. No redirects, no retries, hard cap.

    ``allowed`` is a predicate on the whole URL. The public item fetch passes
    the open-data bucket check. The body is streamed in 64 KiB chunks. More
    than ``max_bytes`` raises :class:`ValueError`. ``total_deadline`` bounds
    the connect and the read. Redirects are not followed and not requested.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("Refusing to fetch an empty URL.")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive.")
    if total_deadline <= 0:
        raise ValueError("total_deadline must be positive.")
    if not allowed(url):
        raise ValueError(f"Refusing to fetch {url!r}: the host is not allowed.")

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

    Uses the shared retrying session. A live catalog walk depends on that
    retry, including a transient S3 503. Public-host item reads call
    :func:`safe_get` themselves.
    """
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
