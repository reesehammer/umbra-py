"""Add ``X-Umbra-Smoke`` to HTTP calls made by the replace-crawler canary.

Python imports this module on startup when its directory is on
``PYTHONPATH``. ``scripts/replace_crawler_canary.py`` sets that only for
the subprocesses that run the documented snippets, so the programs shown
on the docs page stay unchanged and the hosted usage counter ignores them.
"""

from __future__ import annotations

import socket
import urllib.request

HEADER = "X-Umbra-Smoke"
VALUE = "1"


def tag_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Copy ``headers`` with the smoke header set, unless it is already there."""
    merged = dict(headers or {})
    if not any(key.lower() == HEADER.lower() for key in merged):
        merged[HEADER] = VALUE
    return merged


def tag_urllib(
    fullurl: urllib.request.Request | str, data: bytes | None = None
) -> tuple[urllib.request.Request, bytes | None]:
    """Return ``(request, data)`` carrying the smoke header."""
    if isinstance(fullurl, urllib.request.Request):
        if fullurl.get_header("X-umbra-smoke") is None:
            fullurl.add_header(HEADER, VALUE)
        return fullurl, data
    return urllib.request.Request(fullurl, data=data, headers={HEADER: VALUE}), None


def _install_urllib() -> None:
    current = urllib.request.OpenerDirector.open
    if getattr(current, "_umbra_smoke", False):
        return

    def open(self, fullurl, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        fullurl, data = tag_urllib(fullurl, data)
        return current(self, fullurl, data, timeout)

    open._umbra_smoke = True  # type: ignore[attr-defined]
    urllib.request.OpenerDirector.open = open  # type: ignore[method-assign]


def _install_requests() -> None:
    """Tag the prepared request inside ``Session.send``.

    ``pystac-client`` calls ``session.send()`` with a ``PreparedRequest`` and
    never goes through ``Session.request``, so the header has to be on the
    request object that ``send`` already holds.
    """
    try:
        import requests
    except ImportError:
        return
    current = requests.sessions.Session.send
    if getattr(current, "_umbra_smoke", False):
        return

    def send(self, request, **kwargs):
        headers = getattr(request, "headers", None)
        if headers is not None and HEADER not in headers:
            headers[HEADER] = VALUE
        return current(self, request, **kwargs)

    send._umbra_smoke = True  # type: ignore[attr-defined]
    requests.sessions.Session.send = send  # type: ignore[method-assign]


def _install_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return
    current = httpx.Client.send
    if getattr(current, "_umbra_smoke", False):
        return

    def send(self, request, **kwargs):
        if HEADER not in request.headers:
            request.headers[HEADER] = VALUE
        return current(self, request, **kwargs)

    send._umbra_smoke = True  # type: ignore[attr-defined]
    httpx.Client.send = send  # type: ignore[method-assign]


def install() -> None:
    """Patch urllib, requests, and httpx. Idempotent."""
    _install_urllib()
    _install_requests()
    _install_httpx()


install()
