"""Publish rules for the catalog index (PRD P1-2).

The weekly snapshot is the catalog most callers search. A row dated years in
the future, a duplicated STAC id, or a footprint geoparquet cannot read
should not ship: the crawl keeps going (those source items are quarantined
and duplicate ids are collapsed), then the rows that remain are checked
again. A violation that survives that pass fails the build.

Geometry uses a stdlib stand-in for ``shapely.is_valid`` plus ``is_empty``:
a polygon must be closed, finite, non-self-intersecting and have area. Holes
are checked as rings; a hole that falls outside its shell is not rejected
here (Umbra footprints are simple shells). Shapely is not a core dependency.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ._geometry import _bbox_overlaps, _segments_cross
from .catalog import _acq_date
from .exceptions import IndexValidationError, MissingDependencyError
from .models import UmbraItem, _parse_datetime

#: Items dated before Umbra existed are source errors, not acquisitions.
EARLIEST = datetime(2018, 1, 1, tzinfo=timezone.utc)

#: Clock skew and a build that crosses midnight. Strictly after this is future.
FUTURE_SLACK = timedelta(days=1)

#: Square degrees. Below this a ring is a numerical sliver, not a footprint.
_MIN_AREA = 1e-12

#: Substring of Umbra's private processing bucket. Public hrefs never contain it.
PRIVATE_BUCKET = "prod-prod-processed"

#: ``meta`` key holding the last :func:`sanitize_index` report (JSON).
VALIDATION_META_KEY = "validation"

_REASON_BEFORE = "datetime before 2018-01-01"
_REASON_FUTURE = "datetime in the future"
_REASON_MISSING_GEOM = "missing geometry"
_REASON_EMPTY_GEOM = "empty geometry"
_REASON_INVALID_GEOM = "invalid geometry"
_REASON_NO_HTTPS = "no public https asset href"
_REASON_PRIVATE = "private prod-prod-processed href"
_REASON_DUPLICATE = "duplicate id"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return _as_utc(now)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _acquisition_date(item: UmbraItem) -> datetime | None:
    """Midnight UTC of the acquisition directory in ``item.href``, if any.

    That directory date is what the index stores as ``acq_date`` and what a
    date filter prunes on, so a ``2035-...`` folder is "dated in the future"
    even when the STAC ``datetime`` was rewritten.
    """
    href = item.href or ""
    segs = href.rstrip("/").rsplit("/", 2)
    if len(segs) < 2:
        return None
    found = _acq_date(segs[-2])
    if found is None:
        return None
    return datetime(found.year, found.month, found.day, tzinfo=timezone.utc)


def _dated_values(item: UmbraItem) -> list[datetime]:
    values: list[datetime] = []
    if item.datetime is not None:
        values.append(_as_utc(item.datetime))
    acq = _acquisition_date(item)
    if acq is not None:
        values.append(acq)
    return values


def datetime_reasons(item: UmbraItem, *, now: datetime) -> list[str]:
    """Date rules. A missing datetime is not a violation; a bad one is."""
    dates = _dated_values(item)
    reasons: list[str] = []
    if any(value < EARLIEST for value in dates):
        reasons.append(_REASON_BEFORE)
    cutoff = now + FUTURE_SLACK
    if any(value > cutoff for value in dates):
        reasons.append(_REASON_FUTURE)
    return reasons


def _xy(pos: Any) -> tuple[float, float] | None:
    if (
        isinstance(pos, (list, tuple))
        and len(pos) >= 2
        and not isinstance(pos[0], bool)
        and not isinstance(pos[1], bool)
        and isinstance(pos[0], (int, float))
        and isinstance(pos[1], (int, float))
    ):
        x, y = float(pos[0]), float(pos[1])
        if math.isfinite(x) and math.isfinite(y):
            return (x, y)
    return None


def _ring_area(open_pts: list[tuple[float, float]]) -> float:
    total = 0.0
    n = len(open_pts)
    for i in range(n):
        x1, y1 = open_pts[i]
        x2, y2 = open_pts[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _ring_self_intersects(open_pts: list[tuple[float, float]]) -> bool:
    """Non-adjacent edges cross (the shapely ``is_valid`` failure for a shell)."""
    n = len(open_pts)
    if n < 4:
        return False
    for i in range(n):
        a1 = open_pts[i]
        a2 = open_pts[(i + 1) % n]
        ab = (min(a1[0], a2[0]), min(a1[1], a2[1]), max(a1[0], a2[0]), max(a1[1], a2[1]))
        for j in range(i + 1, n):
            if (i + 1) % n == j or (j + 1) % n == i:
                continue
            b1 = open_pts[j]
            b2 = open_pts[(j + 1) % n]
            bb = (min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[0], b2[0]), max(b1[1], b2[1]))
            if not _bbox_overlaps(ab, bb):
                continue
            if _segments_cross(a1, a2, b1, b2):
                return True
    return False


def _ring_reason(ring: Any) -> str | None:
    if not isinstance(ring, list) or not ring:
        return _REASON_EMPTY_GEOM
    if len(ring) < 4:
        return _REASON_INVALID_GEOM
    pts: list[tuple[float, float]] = []
    for pos in ring:
        xy = _xy(pos)
        if xy is None:
            return _REASON_INVALID_GEOM
        pts.append(xy)
    if pts[0] != pts[-1]:
        return _REASON_INVALID_GEOM
    open_pts = pts[:-1]
    if len(open_pts) < 3:
        return _REASON_INVALID_GEOM
    if _ring_self_intersects(open_pts):
        return _REASON_INVALID_GEOM
    if _ring_area(open_pts) <= _MIN_AREA:
        return _REASON_EMPTY_GEOM
    return None


def _polygon_reason(coords: Any) -> str | None:
    if not isinstance(coords, list) or not coords:
        return _REASON_EMPTY_GEOM
    exterior = _ring_reason(coords[0])
    if exterior:
        return exterior
    for hole in coords[1:]:
        hole_reason = _ring_reason(hole)
        if hole_reason:
            # A zero-area hole is a broken shell, not an empty polygon.
            return _REASON_INVALID_GEOM
    return None


def _multipolygon_reason(coords: Any) -> str | None:
    if not isinstance(coords, list) or not coords:
        return _REASON_EMPTY_GEOM
    saw_area = False
    for poly in coords:
        reason = _polygon_reason(poly)
        if reason == _REASON_EMPTY_GEOM:
            continue
        if reason:
            return reason
        saw_area = True
    if not saw_area:
        return _REASON_EMPTY_GEOM
    return None


def _point_reason(coords: Any) -> str | None:
    if coords is None or coords == []:
        return _REASON_EMPTY_GEOM
    if _xy(coords) is None:
        return _REASON_INVALID_GEOM
    return None


def _linestring_reason(coords: Any) -> str | None:
    if not isinstance(coords, list) or not coords:
        return _REASON_EMPTY_GEOM
    if len(coords) < 2 or any(_xy(pos) is None for pos in coords):
        return _REASON_INVALID_GEOM
    return None


def geometry_reason(geom: Any) -> str | None:
    """``None`` when ``geom`` is a non-empty shapely-valid GeoJSON geometry.

    ``None`` (the JSON null) is a missing footprint. An empty coordinate
    array is an empty geometry. Anything else that is not a closed, finite,
    non-self-intersecting polygon (or a non-empty point / line) is invalid.
    """
    if geom is None:
        return _REASON_MISSING_GEOM
    if not isinstance(geom, dict):
        return _REASON_INVALID_GEOM
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        return _polygon_reason(coords)
    if gtype == "MultiPolygon":
        return _multipolygon_reason(coords)
    if gtype == "Point":
        return _point_reason(coords)
    if gtype == "MultiPoint":
        if not isinstance(coords, list) or not coords:
            return _REASON_EMPTY_GEOM
        if any(_xy(pos) is None for pos in coords):
            return _REASON_INVALID_GEOM
        return None
    if gtype == "LineString":
        return _linestring_reason(coords)
    if gtype == "MultiLineString":
        if not isinstance(coords, list) or not coords:
            return _REASON_EMPTY_GEOM
        if any(_linestring_reason(line) for line in coords):
            return _REASON_INVALID_GEOM
        return None
    return _REASON_INVALID_GEOM


def _walk_hrefs(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "href" and isinstance(child, str):
                yield child
            else:
                yield from _walk_hrefs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_hrefs(child)


def _asset_hrefs(item: UmbraItem) -> list[str]:
    hrefs: list[str] = []
    for asset in item.assets.values():
        if isinstance(asset, dict) and isinstance(asset.get("href"), str):
            hrefs.append(asset["href"])
    return hrefs


def _is_private(href: str) -> bool:
    return PRIVATE_BUCKET in href


def _is_public_https(href: str) -> bool:
    return href.startswith("https://") and not _is_private(href)


def asset_reasons(item: UmbraItem) -> list[str]:
    """Public-asset rules, judged on the hrefs the index will publish.

    ``UmbraItem.asset_href`` rewrites an empty or ``s3://`` href at read
    time. The snapshot stores the document as built, so a private bucket
    href that is still in the JSON is a violation even when a reader could
    reconstruct a public sibling.
    """
    reasons: list[str] = []
    asset_hrefs = _asset_hrefs(item)
    if not any(_is_public_https(href) for href in asset_hrefs):
        reasons.append(_REASON_NO_HTTPS)
    everywhere = list(_walk_hrefs(item.raw))
    everywhere.extend(asset_hrefs)
    if isinstance(item.href, str):
        everywhere.append(item.href)
    if any(_is_private(href) for href in everywhere):
        reasons.append(_REASON_PRIVATE)
    return reasons


def item_reasons(item: UmbraItem, *, now: datetime) -> list[str]:
    """Every quarantine reason that applies to ``item`` (empty when it can ship)."""
    reasons = datetime_reasons(item, now=now)
    geom = geometry_reason(item.geometry)
    if geom:
        reasons.append(geom)
    reasons.extend(asset_reasons(item))
    return reasons


def _prop_datetime(item: UmbraItem, key: str) -> datetime | None:
    value = item.properties.get(key)
    if not isinstance(value, str):
        return None
    return _parse_datetime(value)


def _freshness(item: UmbraItem) -> datetime:
    """The timestamp dedupe compares: ``updated``, else ``datetime``, else the folder date.

    A record with none of those sorts as the oldest, so a dated sibling wins.
    """
    updated = _prop_datetime(item, "updated")
    if updated is not None:
        return _as_utc(updated)
    if item.datetime is not None:
        return _as_utc(item.datetime)
    acq = _acquisition_date(item)
    if acq is not None:
        return acq
    return datetime.min.replace(tzinfo=timezone.utc)


def _keep_key(item: UmbraItem) -> tuple:
    """Smaller is the record to keep: latest freshness, then most assets, then href.

    The href tie-break is the stable order: unique in the index, and
    independent of insertion order.
    """
    fresh = _freshness(item)
    return (
        -fresh.year,
        -fresh.month,
        -fresh.day,
        -fresh.hour,
        -fresh.minute,
        -fresh.second,
        -fresh.microsecond,
        -len(item.assets),
        item.href or "",
    )


def _row(item: UmbraItem, **extra: object) -> dict[str, object]:
    return {
        "id": item.id,
        "href": item.href,
        "datetime": _iso(item.datetime),
        **extra,
    }


def iter_index_items(index: Any) -> Iterator[UmbraItem]:
    """Every item in ``index``, href order (the index's stable order)."""
    rows = index._conn.execute("SELECT href, doc, place FROM items ORDER BY href")
    for href, doc, place in rows:
        item = UmbraItem.from_dict(json.loads(doc), href=href)
        item.place = place
        yield item


def collect_violations(items: Iterable[UmbraItem], *, now: datetime | None = None) -> list[dict]:
    """Violations still present. Duplicate ids are a violation on every copy."""
    now = _now(now)
    materialized = list(items)
    counts: dict[str, int] = {}
    for item in materialized:
        counts[item.id] = counts.get(item.id, 0) + 1
    violations: list[dict] = []
    for item in materialized:
        reasons = item_reasons(item, now=now)
        if counts[item.id] > 1:
            reasons.append(_REASON_DUPLICATE)
        if reasons:
            violations.append(_row(item, reasons=reasons))
    return violations


def _delete_hrefs(index: Any, hrefs: list[str]) -> None:
    if not hrefs:
        return
    index._conn.executemany("DELETE FROM item_assets WHERE href = ?", [(href,) for href in hrefs])
    index._conn.executemany("DELETE FROM items WHERE href = ?", [(href,) for href in hrefs])


def sanitize_index(index: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Quarantine bad rows and collapse duplicate ids. Raise if any remain.

    Known-bad source items are removed and recorded; they do not abort the
    crawl. The rows left behind are checked with :func:`collect_violations`.
    A hit there means the quarantine missed something, and the build must
    not publish — :class:`~umbra_py.IndexValidationError`.

    The report is stored on the index under :data:`VALIDATION_META_KEY` so
    the coverage report can list what was dropped. Dedupe keeps the latest
    ``properties.updated`` (falling back to ``datetime``, then the
    acquisition-directory date), then the record with the most assets, then
    the lowest href.
    """
    now = _now(now)
    items = list(iter_index_items(index))
    quarantined: list[dict[str, object]] = []
    survivors: list[UmbraItem] = []
    for item in items:
        reasons = item_reasons(item, now=now)
        if reasons:
            quarantined.append(_row(item, reasons=reasons))
        else:
            survivors.append(item)

    duplicates: list[dict[str, object]] = []
    drop_hrefs = [row["href"] for row in quarantined if isinstance(row["href"], str)]
    by_id: dict[str, list[UmbraItem]] = {}
    for item in survivors:
        by_id.setdefault(item.id, []).append(item)
    for group in by_id.values():
        if len(group) < 2:
            continue
        group.sort(key=_keep_key)
        winner = group[0]
        for loser in group[1:]:
            duplicates.append(_row(loser, kept_href=winner.href))
            if loser.href:
                drop_hrefs.append(loser.href)

    quarantined.sort(key=lambda row: (str(row["href"] or ""), str(row["id"])))
    duplicates.sort(key=lambda row: (str(row["href"] or ""), str(row["id"])))
    _delete_hrefs(index, [href for href in drop_hrefs if isinstance(href, str)])

    violations = collect_violations(iter_index_items(index), now=now)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checked_at": now.isoformat(),
        "earliest": EARLIEST.isoformat(),
        "future_after": (now + FUTURE_SLACK).isoformat(),
        "input_rows": len(items),
        "kept": len(items) - len(quarantined) - len(duplicates),
        "quarantined_count": len(quarantined),
        "quarantined": quarantined,
        "duplicates_dropped": len(duplicates),
        "duplicates": duplicates,
        "violations": violations,
    }
    index.set_meta(VALIDATION_META_KEY, json.dumps(report))
    index._conn.commit()
    if violations:
        raise IndexValidationError(
            _violation_message(violations),
            hint="The index was quarantined and re-checked; a remaining violation "
            "means the snapshot must not be published. See the validation section "
            "of the coverage report.",
        )
    return report


def _violation_message(violations: list[dict]) -> str:
    shown = violations[:5]
    bits = [
        f"{row.get('id') or row.get('href')} ({', '.join(row.get('reasons') or [])})"
        for row in shown
    ]
    extra = len(violations) - len(shown)
    suffix = f" (+{extra} more)" if extra else ""
    return (
        f"{len(violations)} item(s) still violate catalog index rules after quarantine: "
        + "; ".join(bits)
        + suffix
    )


def stored_validation(index: Any) -> dict[str, Any] | None:
    """The report :func:`sanitize_index` stored, or ``None`` if this index has none."""
    raw = index.get_meta(VALIDATION_META_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def parquet_violations(path: str, *, now: datetime | None = None) -> list[dict]:
    """The same rules, read back from a stac-geoparquet export.

    Requires the ``export`` extra. Items the writer skipped never appear;
    every row that did is held to the publish rules.
    """
    try:
        import pyarrow.parquet as pq
        import stac_geoparquet.arrow as arrow
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise MissingDependencyError(
            "stac-geoparquet is required to validate a parquet export. "
            'Install the extra with: pip install "umbra-py[export]"',
            hint='pip install "umbra-py[export]"',
        ) from exc
    docs = list(arrow.stac_table_to_items(pq.read_table(path)))
    items = [UmbraItem.from_dict(doc) for doc in docs]
    return collect_violations(items, now=_now(now))


def assert_recheck_clean(report: dict[str, Any]) -> None:
    """Raise when a :func:`recheck_report` still finds a publish-rule violation."""
    recheck = report.get("recheck") or {}
    violations = list(recheck.get("index_violations") or [])
    parquet = recheck.get("parquet_violations")
    if parquet:
        violations.extend(parquet)
    if violations:
        raise IndexValidationError(
            _violation_message(violations),
            hint="umbra index build quarantines known-bad source items. A violation "
            "that is still in the index or the parquet fails the publish.",
        )


def recheck_report(
    index: Any,
    *,
    parquet: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stored quarantine report plus a fresh check of the index and parquet."""
    now = _now(now)
    stored = stored_validation(index) or {
        "generated_at": None,
        "checked_at": None,
        "earliest": EARLIEST.isoformat(),
        "future_after": (now + FUTURE_SLACK).isoformat(),
        "input_rows": None,
        "kept": None,
        "quarantined_count": 0,
        "quarantined": [],
        "duplicates_dropped": 0,
        "duplicates": [],
        "violations": [],
    }
    recheck: dict[str, Any] = {
        "checked_at": now.isoformat(),
        "index_violations": collect_violations(iter_index_items(index), now=now),
    }
    if parquet is not None:
        recheck["parquet_violations"] = parquet_violations(parquet, now=now)
    report = dict(stored)
    report["recheck"] = recheck
    return report


def render_validation_section(report: dict[str, Any]) -> str:
    """Markdown section (no document title) listing quarantined rows and dedupe."""
    lines = [
        f"Quarantined {report.get('quarantined_count', 0)} item(s). "
        f"Dropped {report.get('duplicates_dropped', 0)} duplicate id(s).",
        "",
    ]
    quarantined = report.get("quarantined") or []
    if quarantined:
        lines += ["| id | href | reasons |", "| --- | --- | --- |"]
        for row in quarantined:
            reasons = ", ".join(row.get("reasons") or [])
            lines.append(f"| {row.get('id', '')} | {row.get('href', '')} | {reasons} |")
        lines.append("")
    duplicates = report.get("duplicates") or []
    if duplicates:
        lines += [
            "| duplicate id | dropped href | kept href |",
            "| --- | --- | --- |",
        ]
        for row in duplicates:
            lines.append(
                f"| {row.get('id', '')} | {row.get('href', '')} | {row.get('kept_href', '')} |"
            )
        lines.append("")
    recheck = report.get("recheck")
    if isinstance(recheck, dict):
        lines.append(
            f"Recheck: {len(recheck.get('index_violations') or [])} index violation(s) remain."
        )
        if recheck.get("parquet_violations") is not None:
            lines.append(f"Parquet: {len(recheck['parquet_violations'])} violation(s).")
        lines.append("")
    return "\n".join(lines)


def render_validation_markdown(report: dict[str, Any]) -> str:
    """A standalone validation report."""
    return (
        "# umbra-py index validation\n\n"
        + render_validation_section(report)
        + "Contains Umbra open data, licensed under CC BY 4.0.\n"
    )


def write_validation_report(
    report: dict[str, Any], json_path: str | None, md_path: str | None
) -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    if md_path:
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_validation_markdown(report))
