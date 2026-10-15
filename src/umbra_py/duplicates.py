"""Link republished copies of one Umbra collect without dropping any of them.

Umbra publishes the same capture under more than one S3 prefix, usually
``sar-data/task-data/<task_id>/...`` and
``sar-data/tasks/<campaign>/<task_id>/...``. Those sidecars are different
STAC items (different ids, often a different processor version, sometimes a
different product set) that share ``umbra:task_id`` and ``umbra:collect_id``.

The index keeps every copy. This pass groups the ones that are the same
imaging window, marks one canonical, and writes that choice onto each item
so a search can keep a single copy without a row being deleted.

Canonical copy, first key that differs wins:

1. Newest ``properties.created``. A missing ``created`` loses to any present
   one. When ``created`` ties, fall back to the next key.
2. Higher ``processing:version`` (dotted integers, so ``5.2.10`` beats
   ``5.2.8``). When that is absent, the version string inside
   ``processing:software``. A missing version loses to any present one.
3. Newest acquisition ``datetime``.
4. Most canonical product assets present (``GEC``, ``CSI``, ``SIDD``,
   ``SICD``, ``CPHD``). CPHD counts as one of those products, so the copy
   that actually carries CPHD wins a tie the earlier keys did not break.
5. Prefer an href under ``sar-data/task-data/``.
6. Lowest STAC id.

A group is one capture only when every member's ``datetime`` is within 60
seconds of the earliest in that cluster. A wider gap is a different imaging
window and is split, even when the task and collect ids match. Items with
no ``datetime`` are not grouped: the window cannot be checked.

``duplicate_confidence`` is ``high`` only for a pair that is one
``task-data`` href and one ``tasks/<campaign>`` href. Every other linked
group — all copies under ``task-data``, a third copy, an ``open-data`` or
other href — is ``low`` so it can be reviewed. Those groups are still linked.

Link relations follow the IANA registry with their registered meaning.
``rel: canonical`` is the preferred version of the resource. ``rel: related``
points at each sibling. IANA ``duplicate`` means a byte-for-byte identical
representation (RFC 6249); these copies are not that — the ids differ, the
processor version often differs, and one copy may be the only one with
CPHD — so ``related`` is the relation that does not claim the bytes are
interchangeable. ``umbra-py:duplicate_ids`` is what names them as copies.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import UmbraItem

#: Copies whose acquisition times differ by more than this are different windows.
DUPLICATE_WINDOW = timedelta(seconds=60)

CANONICAL_PROP = "umbra-py:canonical"
CANONICAL_ID_PROP = "umbra-py:canonical_id"
DUPLICATE_IDS_PROP = "umbra-py:duplicate_ids"
DUPLICATE_CONFIDENCE_PROP = "umbra-py:duplicate_confidence"

#: IANA: the preferred version of this resource.
REL_CANONICAL = "canonical"
#: IANA: another resource. Not IANA ``duplicate`` — these copies are not byte-identical.
REL_RELATED = "related"

_ANNOTATION_PROPS = frozenset(
    {
        CANONICAL_PROP,
        CANONICAL_ID_PROP,
        DUPLICATE_IDS_PROP,
        DUPLICATE_CONFIDENCE_PROP,
    }
)
#: Relations this pass owns. Stripped before a re-link so a second build does
#: not accumulate links. Umbra's own sidecars use root / parent / self / collection.
PRESERVED_LINK_RELS = frozenset({REL_CANONICAL, REL_RELATED})

_LINK_TYPE = "application/json"


def prefix_kind(href: str | None) -> str:
    """``task-data``, ``tasks``, or ``other`` from a sidecar href.

    ``other`` is anything that is not the two open-data layouts the republish
    usually uses (an ``open-data/`` key, a Canopy URL, a missing href).
    """
    if not href:
        return "other"
    if "/sar-data/task-data/" in href:
        return "task-data"
    if "/sar-data/tasks/" in href:
        return "tasks"
    return "other"


def canonical_flag(item: UmbraItem) -> int:
    """1 when this row is the copy to keep, including items not in a group.

    Absent means canonical. The index column uses that so a snapshot linked
    by an older build still answers ``canonical=true`` with every row until
    the next link pass, which is the same answer as "not in a group".
    """
    return 0 if item.properties.get(CANONICAL_PROP) is False else 1


def passes_canonical_filter(item: UmbraItem, canonical: bool | None) -> bool:
    """Whether ``item`` survives a ``canonical`` filter.

    ``None`` keeps everything. A missing property counts as canonical, matching
    :func:`canonical_flag` for a live item that has not been through the linker.
    """
    if canonical is None:
        return True
    return (canonical_flag(item) == 1) is canonical


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_version(value: Any) -> tuple[int, ...] | None:
    """Dotted integers from a processor version string. ``5.2.10`` > ``5.2.8``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    parts: list[int] = []
    for piece in text.split("."):
        digits: list[str] = []
        for ch in piece:
            if not ch.isdigit():
                break
            digits.append(ch)
        if not digits:
            break
        parts.append(int("".join(digits)))
        if len(digits) != len(piece):
            break
    return tuple(parts) if parts else None


def _processor_version(item: UmbraItem) -> tuple[int, ...] | None:
    parsed = _parse_version(item.properties.get("processing:version"))
    if parsed is not None:
        return parsed
    software = item.properties.get("processing:software")
    if isinstance(software, dict):
        for value in software.values():
            parsed = _parse_version(value)
            if parsed is not None:
                return parsed
    return None


def _time_key(value: datetime | None) -> tuple[int, float]:
    """Present times sort after missing ones; newer sorts after older."""
    if value is None:
        return (0, 0.0)
    return (1, _as_utc(value).timestamp())


def _rank_key(item: UmbraItem) -> tuple[Any, ...]:
    version = _processor_version(item)
    version_key = (1, version) if version is not None else (0, ())
    return (
        _time_key(item.created),
        version_key,
        _time_key(item.datetime),
        len(item.available_assets),
        1 if prefix_kind(item.href) == "task-data" else 0,
    )


def choose_canonical(items: Sequence[UmbraItem]) -> UmbraItem:
    """The preferred copy. See the module docstring for the key order.

    Ties fall through to the next key. The last tie breaks on the lowest id.
    Sorting by id first and then by rank (stable) keeps that id first among
    equal ranks.
    """
    ordered = sorted(items, key=lambda item: item.id)
    ordered.sort(key=_rank_key, reverse=True)
    return ordered[0]


def duplicate_confidence(items: Sequence[UmbraItem]) -> str:
    """``high`` for one task-data copy and one tasks/campaign copy; else ``low``."""
    kinds = [prefix_kind(item.href) for item in items]
    if (
        len(items) == 2
        and kinds.count("task-data") == 1
        and kinds.count("tasks") == 1
        and all(item.datetime is not None for item in items)
    ):
        return "high"
    return "low"


def _capture_key(item: UmbraItem) -> tuple[str, str] | None:
    task = item.properties.get("umbra:task_id")
    collect = item.properties.get("umbra:collect_id")
    if not isinstance(task, str) or not task.strip():
        return None
    if not isinstance(collect, str) or not collect.strip():
        return None
    return (task, collect)


def _clusters(items: Sequence[UmbraItem]) -> list[list[UmbraItem]]:
    """Split one (task, collect) bucket into windows of at most 60 seconds.

    The span is measured from the earliest datetime in the cluster, so a chain
    of 50-second steps does not merge windows that are minutes apart. Items
    with no datetime are left out: they cannot be shown to share a window.
    """
    dated: list[tuple[datetime, UmbraItem]] = []
    for item in items:
        dt = item.datetime
        if dt is None:
            continue
        dated.append((_as_utc(dt), item))
    dated.sort(key=lambda pair: (pair[0], pair[1].id))
    clusters: list[list[UmbraItem]] = []
    current: list[UmbraItem] = []
    start: datetime | None = None
    for dt, item in dated:
        if start is None or dt - start > DUPLICATE_WINDOW:
            if current:
                clusters.append(current)
            current = [item]
            start = dt
        else:
            current.append(item)
    if current:
        clusters.append(current)
    return clusters


def duplicate_groups(items: Iterable[UmbraItem]) -> list[list[UmbraItem]]:
    """Groups of two or more copies of one collect inside a 60-second window."""
    buckets: dict[tuple[str, str], list[UmbraItem]] = {}
    for item in items:
        key = _capture_key(item)
        if key is None:
            continue
        buckets.setdefault(key, []).append(item)
    groups: list[list[UmbraItem]] = []
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for cluster in _clusters(bucket):
            if len(cluster) >= 2:
                groups.append(cluster)
    return groups


def _clean_props(item: UmbraItem) -> dict[str, Any]:
    return {key: value for key, value in item.properties.items() if key not in _ANNOTATION_PROPS}


def _clean_links(item: UmbraItem) -> list[dict[str, Any]]:
    links = item.raw.get("links") if item.raw else None
    if not isinstance(links, list):
        return []
    return [
        link
        for link in links
        if not (isinstance(link, dict) and link.get("rel") in PRESERVED_LINK_RELS)
    ]


def _bind(item: UmbraItem, props: dict[str, Any], links: list[dict[str, Any]]) -> None:
    item.properties = props
    raw: dict[str, Any] = dict(item.raw) if item.raw else {"type": "Feature", "id": item.id}
    raw["id"] = item.id
    raw["properties"] = props
    raw["links"] = links
    item.raw = raw


def _link(rel: str, href: str, title: str) -> dict[str, str]:
    return {"rel": rel, "href": href, "type": _LINK_TYPE, "title": title}


def _stamp_group(group: Sequence[UmbraItem], winner: UmbraItem, confidence: str) -> None:
    for item in group:
        props = _clean_props(item)
        props[CANONICAL_PROP] = item.id == winner.id
        props[CANONICAL_ID_PROP] = winner.id
        props[DUPLICATE_IDS_PROP] = sorted(other.id for other in group if other.id != item.id)
        props[DUPLICATE_CONFIDENCE_PROP] = confidence
        links = _clean_links(item)
        if item.id != winner.id and winner.href:
            links.append(_link(REL_CANONICAL, winner.href, winner.id))
        for other in sorted(group, key=lambda member: member.id):
            if other.id == item.id or not other.href:
                continue
            links.append(_link(REL_RELATED, other.href, other.id))
        _bind(item, props, links)


def _stamp_singleton(item: UmbraItem) -> None:
    props = _clean_props(item)
    props[CANONICAL_PROP] = True
    _bind(item, props, _clean_links(item))


def annotate_republished(items: Iterable[UmbraItem]) -> dict[str, int]:
    """Stamp canonical fields and links on ``items`` in place.

    Every item gets ``umbra-py:canonical``. Items that are not in a duplicate
    group get no other duplicate fields, so a consumer filters on the one
    boolean. Returns the summary written into the validation report:
    ``groups``, ``extra_items`` (copies beyond the canonical one),
    ``unique_captures``, and ``low_confidence_groups``.
    """
    materialized = list(items)
    groups = duplicate_groups(materialized)
    grouped: set[int] = set()
    low = 0
    extra = 0
    for group in groups:
        winner = choose_canonical(group)
        confidence = duplicate_confidence(group)
        if confidence == "low":
            low += 1
        extra += len(group) - 1
        for item in group:
            grouped.add(id(item))
        _stamp_group(group, winner, confidence)
    for item in materialized:
        if id(item) not in grouped:
            _stamp_singleton(item)
    return {
        "groups": len(groups),
        "extra_items": extra,
        "unique_captures": len(materialized) - extra,
        "low_confidence_groups": low,
    }
