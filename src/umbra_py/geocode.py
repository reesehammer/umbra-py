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
