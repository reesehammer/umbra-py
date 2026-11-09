"""Forward geocoding: resolve a free-text place name to a bounding box.

The discovery commands' ``--place`` option lets a user search by a human place
name ("California", "Tokyo") instead of hand-typing a bounding box. This module
calls OpenStreetMap's Nominatim search endpoint and returns the place's
bounding box in the ``(min_lon, min_lat, max_lon, max_lat)`` order
:meth:`umbra_py.UmbraCatalog.search` expects.

The *reverse* direction (coordinates -> place name) lives in :mod:`umbra_py.viz`
for map popups; both honor Nominatim's usage policy via the shared,
descriptively user-agented HTTP session.

Note: a bounding box is rectangular, so searching ``"California"`` also matches
footprints in the box's corners that fall outside the state's true outline
(slivers of neighboring states, ocean). That's the same bbox-overlap semantics
the rest of the search uses; pass an explicit ``--bbox`` for a tighter window.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import requests

from .exceptions import GeocodeError

# (min_lon, min_lat, max_lon, max_lat)
BBox = tuple[float, float, float, float]

# OpenStreetMap's Nominatim forward-geocoding endpoint. Its usage policy
# requires a descriptive User-Agent (the shared session supplies one); a
# ``--place`` search makes a single call per invocation.
_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"


def geocode_place(
    query: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> tuple[BBox, str]:
    """Resolve a place name to its ``(bbox, display_name)``.

    ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)`` -- the order
    :meth:`umbra_py.UmbraCatalog.search` expects. ``display_name`` is
    Nominatim's full label for the match (e.g. ``"California, United States"``)
    so callers can confirm what was resolved.

    Raises :class:`~umbra_py.exceptions.GeocodeError` if the query is empty, the
    place can't be found, or the service is unreachable / returns malformed
    data.
    """
    query = (query or "").strip()
    if not query:
        raise GeocodeError("Empty place name.")

    if session is None:
        from ._http import default_session  # noqa: PLC0415

        session = default_session()

    try:
        params: dict[str, str | int] = {"q": query, "format": "jsonv2", "limit": 1}
        resp = session.get(
            _NOMINATIM_SEARCH_URL,
            params=params,
            timeout=timeout,
            headers={"Accept-Language": "en"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise GeocodeError(f"Could not reach the geocoding service: {exc}") from exc
    except ValueError as exc:  # non-JSON body
        raise GeocodeError("Geocoding service returned a malformed response.") from exc

    if not payload:
        raise GeocodeError(
            f"No place matched {query!r}. Try a more specific name, or pass --bbox.",
            hint="Pass an explicit --bbox 'W,S,E,N' instead of --place",
        )

    match = payload[0]
    bbox = _parse_boundingbox(match.get("boundingbox"))
    if bbox is None:
        raise GeocodeError(f"Geocoding {query!r} returned no usable bounding box.")
    label = match.get("display_name") or query
    return bbox, str(label)


def _parse_boundingbox(raw: Any) -> BBox | None:
    """Convert Nominatim's ``[south, north, west, east]`` to our bbox order.

    Nominatim returns the box as four strings ``[min_lat, max_lat, min_lon,
    max_lon]``; we reorder to ``(min_lon, min_lat, max_lon, max_lat)`` and
    coerce to float. Returns ``None`` if the shape is unexpected.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        south, north, west, east = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (west, south, east, north)


# A miss that would wait longer than this, or join more than this many
# callers already waiting on Nominatim, fails immediately. Same bounds as
# the public-host gate in PR #318: one in-flight call, then the one-second
# gap, and that whole wait stays under about five seconds.
_GEOCODE_MAX_WAIT_S = 5.0
_GEOCODE_MAX_WAITERS = 5
#: How long a "no such place" answer is remembered. A network failure is not.
_GEOCODE_NEGATIVE_TTL_S = 600.0
_GEOCODE_BUSY = "Geocoding is busy on this host; retry in a few seconds."


def _geocode_not_found(exc: GeocodeError) -> bool:
    text = str(exc)
    return text.startswith("No place matched") or "no usable bounding box" in text


class PublicGeocodeGate:
    """LRU cache and a global Nominatim throttle for the public host.

    One shared address is what Nominatim bans. Cache hits do not wait and
    do not call out. A miss waits for the single in-flight call and for the
    one-second gap, and that whole wait is capped at about five seconds.
    More than five waiters, or a lock that is not free inside the remaining
    budget, fails immediately. "No such place" is remembered for about ten
    minutes. Any other failure is not cached. Local servers do not use this.

    PR #318 adds this same gate inside ``mcp_server.py``. It lives here so
    ``/search``, ``/sites``, and the MCP tools share one cache and one
    one-request-per-second slot. On merge with that PR, delete the copy in
    ``mcp_server.py`` and keep this one.
    """

    def __init__(
        self,
        *,
        max_size: int = 256,
        min_interval: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_size = max_size
        self._min_interval = min_interval
        self._time = time_fn
        self._sleep = sleep_fn
        self._lock = threading.Lock()
        self._network = threading.Lock()
        self._cache: OrderedDict[str, tuple[Any, ...]] = OrderedDict()
        self._next_at = 0.0
        self._waiters = 0

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._next_at = 0.0
            self._waiters = 0

    def resolve(self, query: str) -> tuple[BBox, str]:
        key = " ".join(query.split()).casefold()
        cached = self._cached(key)
        if cached is not None:
            return cached
        deadline = self._time() + _GEOCODE_MAX_WAIT_S
        self._admit()
        acquired = False
        try:
            remaining = deadline - self._time()
            if remaining <= 0 or not self._network.acquire(timeout=remaining):
                raise GeocodeError(_GEOCODE_BUSY)
            acquired = True
            cached = self._cached(key)
            if cached is not None:
                return cached
            now = self._time()
            throttle = max(0.0, self._next_at - now)
            if throttle > deadline - now:
                raise GeocodeError(_GEOCODE_BUSY)
            self._next_at = now + throttle + self._min_interval
            if throttle > 0:
                self._sleep(throttle)
            try:
                result = geocode_place(query)
            except GeocodeError as exc:
                if _geocode_not_found(exc):
                    self._store(key, ("neg", self._time() + _GEOCODE_NEGATIVE_TTL_S, str(exc)))
                raise
            self._store(key, ("ok", result))
            return result
        finally:
            if acquired:
                self._network.release()
            with self._lock:
                self._waiters -= 1

    def _admit(self) -> None:
        with self._lock:
            if self._waiters >= _GEOCODE_MAX_WAITERS:
                raise GeocodeError(_GEOCODE_BUSY)
            self._waiters += 1

    def _cached(self, key: str) -> tuple[BBox, str] | None:
        with self._lock:
            return self._cached_locked(key)

    def _cached_locked(self, key: str) -> tuple[BBox, str] | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if hit[0] == "neg":
            if self._time() >= hit[1]:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            raise GeocodeError(hit[2])
        self._cache.move_to_end(key)
        return hit[1]

    def _store(self, key: str, entry: tuple[Any, ...]) -> None:
        with self._lock:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)


_public_geocode = PublicGeocodeGate()


def geocode_for_host(query: str, *, public: bool) -> tuple[BBox, str]:
    """Resolve ``query`` directly, or through :class:`PublicGeocodeGate` on the public host.

    Local MCP and ``umbra serve`` call Nominatim on each miss. ``umbra serve
    --public`` and the public MCP host share :data:`_public_geocode`, so a
    cached place is not requested twice and a burst fails fast instead of
    queueing on Nominatim.
    """
    if not public:
        return geocode_place(query)
    return _public_geocode.resolve(query)
