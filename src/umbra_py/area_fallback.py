"""When ``area=`` names no Umbra task, search the geocoded place instead.

``area`` is a task-directory substring. Callers who type a city or a
company ("Austin", "TSMC") get an empty page today, because no task
directory contains that string. This module is the HTTP and MCP decision
in front of the existing ``place=`` geocoder: a task-name hit is left
untouched, and only a miss with no other footprint is geocoded.

An explicit ``bbox``, ``intersects``, or ``place`` is already a footprint.
``area`` stays a task filter then, which is also how ``--place`` and
``--bbox`` refuse to combine on the CLI. The GeoJSON ``intersects`` filter
(the sibling change on these same handlers) is that explicit footprint:
when it is set, this module does not geocode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .exceptions import GeocodeError
from .geocode import BBox

Geocoder = Callable[[str], tuple[BBox, str]]


@dataclass(frozen=True)
class AreaFallback:
    """How one ``area`` string should be searched.

    ``matched_by`` is None when the caller should search exactly as it does
    today (no ``area``, a task-name hit, or ``area`` beside an explicit
    footprint). ``"geocode"`` means no task matched and Nominatim resolved a
    footprint: search ``bbox`` and do not also filter by the task name.
    ``hint`` is set when no task matched and the geocoder missed too; the
    caller returns that instead of an empty result.
    """

    area: str | None
    bbox: BBox | None
    matched_by: str | None = None
    place: str | None = None
    hint: str | None = None


def _place_miss(exc: GeocodeError) -> bool:
    """A name Nominatim will not turn into a box, as opposed to a busy host."""
    text = str(exc)
    return (
        text.startswith("No place matched")
        or text.startswith("Empty place name")
        or "no usable bounding box" in text
    )


def _area_matches_task(source: Any, area: str, *, fuzzy: bool) -> bool:
    """Whether ``area`` selects at least one task on ``source``.

    Index and live catalog implement ``has_matching_task`` with the same
    rule as their ``search``. A test double that only has ``search`` is
    asked for one item; anything it yields counts as a hit, which is what
    those doubles already do (they do not filter).
    """
    method = getattr(source, "has_matching_task", None)
    if callable(method):
        return bool(method(area, fuzzy=fuzzy))
    stream = source.search(area=area, fuzzy=fuzzy, limit=1)
    try:
        return next(stream, None) is not None
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def resolve_area_fallback(
    source: Any,
    area: str | None,
    *,
    fuzzy: bool,
    bbox: BBox | None,
    intersects: Any,
    place: str | None,
    geocode: Geocoder,
) -> AreaFallback:
    """Choose a task filter, a geocoded footprint, or a miss hint.

    ``geocode`` is the host's Nominatim call (direct locally, the shared
    public gate on ``umbra serve --public``). It is not called when ``area``
    is absent, when a task name matches, or when the request already has a
    footprint.
    """
    if not area:
        return AreaFallback(area=None, bbox=None)
    if bbox is not None or intersects is not None or place:
        return AreaFallback(area=area, bbox=None)
    if _area_matches_task(source, area, fuzzy=fuzzy):
        return AreaFallback(area=area, bbox=None)
    try:
        box, label = geocode(area)
    except GeocodeError as exc:
        if _place_miss(exc):
            return AreaFallback(
                area=None,
                bbox=None,
                hint=f"no task named {area}; try place= or bbox=",
            )
        raise
    return AreaFallback(area=None, bbox=box, matched_by="geocode", place=str(label))


def geocode_note(decision: AreaFallback) -> dict[str, Any] | None:
    """JSON fields added only when ``area`` was geocoded. A task hit adds none."""
    if decision.matched_by != "geocode":
        return None
    return {
        "matched_by": decision.matched_by,
        "resolved_place": decision.place,
        "resolved_bbox": list(decision.bbox) if decision.bbox else None,
    }
