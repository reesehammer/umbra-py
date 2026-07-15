"""Small shared HTTP helpers built on :mod:`requests`.

Umbra's open data is served over plain, anonymous HTTPS (both the STAC catalog
JSON and the data assets), so we never need AWS credentials or signed requests.
"""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

DEFAULT_TIMEOUT = 30

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


def get_json(url: str, session: requests.Session | None = None, **kwargs: Any) -> dict:
    """Fetch and decode a JSON document."""
    sess = session or default_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    resp = sess.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()
