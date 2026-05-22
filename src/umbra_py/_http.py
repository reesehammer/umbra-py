"""Small shared HTTP helpers built on :mod:`requests`.

Umbra's open data is served over plain, anonymous HTTPS (both the STAC catalog
JSON and the data assets), so we never need AWS credentials or signed requests.
"""

from __future__ import annotations

from typing import Any

import requests

from . import __version__

DEFAULT_TIMEOUT = 30


def default_session() -> requests.Session:
    """Return a :class:`requests.Session` with a descriptive user agent."""
    session = requests.Session()
    session.headers.update({"User-Agent": f"umbra-py/{__version__}"})
    return session


def get_json(url: str, session: requests.Session | None = None, **kwargs: Any) -> dict:
    """Fetch and decode a JSON document."""
    sess = session or default_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    resp = sess.get(url, **kwargs)
    resp.raise_for_status()
    return resp.json()
