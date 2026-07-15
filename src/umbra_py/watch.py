"""umbra watch: idempotent delta detection for standing site monitoring.

SAR's value for monitoring is its cadence -- a site is re-imaged pass after
pass -- so the natural agentic workflow is *standing*: run the same search on a
schedule and act only on what is **new** since last time. This module packages
the delta, not the schedule. The scheduler (cron, a GitHub Action, an agent
framework's loop) supplies the "when"; :func:`watch` supplies the idempotent
"what changed", so a run that finds nothing new is a clean no-op and a run that
finds three new acquisitions says exactly which three -- every time, without
re-alerting on acquisitions a previous run already reported.

Design, following the package's determinism boundary
(``docs/AI_INTEGRATION_IDEAS.md`` C3, §6.1):

- **The search source is injected.** :func:`watch` takes anything with a
  :meth:`search` mirroring :meth:`UmbraCatalog.search` -- a live
  :class:`~umbra_py.UmbraCatalog` (the usual choice: monitoring wants freshly
  published acquisitions) or a :class:`~umbra_py.CatalogIndex` (to diff two
  index snapshots). No model is ever called; this is pure set arithmetic over
  the deterministic search the library already does.
- **State is a small, explicit store.** A watch remembers the set of
  acquisition keys (sidecar hrefs) it has already reported, keyed by a stable
  watch *name*. :class:`MetaWatchStore` persists that set in a
  :class:`~umbra_py.CatalogIndex`'s ``meta`` table -- no schema change, so it
  works against a freshly ``umbra index fetch``-ed snapshot -- and
  :class:`InMemoryWatchStore` is the ephemeral, offline-testable stand-in.
- **Delta by exact set difference, not a date watermark.** New acquisitions can
  be published for an *earlier* date than ones already seen (a late upload), so
  a "max date seen" watermark would miss them. Comparing the full key set is
  exact and truly idempotent: re-running with no new data reports zero.

The result (:class:`WatchResult`) is machine-readable first: ``umbra watch
--json`` emits ``{new_count, new_items: [context cards], ...}`` for a scheduler
to branch on, and ``--exit-code`` turns "are there new acquisitions?" into a
process exit status a shell ``if`` can test. Every JSON payload carries the
CC-BY attribution, like every other data-bearing surface in the library.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from .constants import ATTRIBUTION, DATA_LICENSE
from .models import UmbraItem

_META_PREFIX = "watch:"


@runtime_checkable
class SearchSource(Protocol):
    """Anything that searches like :meth:`UmbraCatalog.search`.

    Both :class:`~umbra_py.UmbraCatalog` (live S3 walk) and
    :class:`~umbra_py.CatalogIndex` (local SQL) satisfy this, so a watch can run
    against either without change.
    """

    def search(self, **kwargs: Any) -> Iterator[UmbraItem]: ...


@runtime_checkable
class WatchStore(Protocol):
    """Persistence for a watch's already-reported acquisition keys.

    :meth:`load` returns ``None`` for a watch with no recorded state (so the
    first run is distinguishable from a run that has legitimately seen nothing),
    and a set of keys otherwise. :meth:`save` records the updated set.
    """

    def load(self, name: str) -> set[str] | None: ...

    def save(self, name: str, keys: set[str]) -> None: ...


class InMemoryWatchStore:
    """A :class:`WatchStore` kept in a dict -- ephemeral, no persistence.

    Handy for tests and one-shot in-process use where the delta only needs to
    be correct within a single run.
    """

    def __init__(self) -> None:
        self._store: dict[str, set[str]] = {}

    def load(self, name: str) -> set[str] | None:
        seen = self._store.get(name)
        return set(seen) if seen is not None else None

    def save(self, name: str, keys: set[str]) -> None:
        self._store[name] = set(keys)


class MetaWatchStore:
    """A :class:`WatchStore` backed by a :class:`~umbra_py.CatalogIndex`.

    The seen-key set is stored as a JSON document in the index's ``meta`` table
    under ``watch:<name>``. This deliberately reuses the existing metadata table
    rather than adding a schema -- so watch state persists in the same
    ``catalog.db`` a user already builds or fetches, and a prebuilt snapshot
    stays a valid state store with no migration. The index is used only for its
    key/value store here; the acquisition rows are untouched.
    """

    def __init__(self, index: Any) -> None:
        self._index = index

    def load(self, name: str) -> set[str] | None:
        raw = self._index.get_meta(_META_PREFIX + name)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # A corrupt/legacy value should not crash a scheduled run; treat it
            # as "no baseline" so the next run re-establishes one cleanly.
            return None
        seen = data.get("seen") if isinstance(data, dict) else data
        return {str(k) for k in seen} if seen else set()

    def save(self, name: str, keys: set[str]) -> None:
        self._index.set_meta(_META_PREFIX + name, json.dumps({"seen": sorted(keys)}))
        self._index.commit()


def _item_key(item: UmbraItem) -> str | None:
    """Stable identity for delta comparison: the sidecar href (unique within the
    bucket), falling back to the item id. ``None`` if the item has neither."""
    return item.href or item.id or None


def _clean_query(search_kwargs: dict[str, Any]) -> dict[str, Any]:
    """A JSON-serializable echo of the search that produced the delta, for
    auditability in the ``--json`` output. Drops unset filters and normalizes
    tuples (a bbox) to lists."""
    out: dict[str, Any] = {}
    for key, value in search_kwargs.items():
        if value is None or value == () or value == []:
            continue
        out[key] = list(value) if isinstance(value, tuple) else value
    return out


def watch_key(
    *,
    area: str | None = None,
    place: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    product_types: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    fuzzy: bool = False,
) -> str:
    """Derive a stable, readable watch name from a query.

    Used when the caller does not name a watch explicitly: the same query always
    yields the same name (so repeat runs line up on the same stored state), and
    two different queries essentially never collide. The name is a human-legible
    slug of the area/place plus a short hash of the full normalized query, e.g.
    ``centerfield-utah-3f9a1c2e``.
    """
    params = _clean_query(
        {
            "area": area,
            "place": place,
            "bbox": bbox,
            "product_types": sorted(product_types) if product_types else None,
            "start": start,
            "end": end,
            "fuzzy": fuzzy or None,
        }
    )
    canonical = json.dumps(params, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    label = area or place or ("bbox" if bbox else "all")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "watch"
    return f"{slug}-{digest}"


@dataclass
class WatchResult:
    """The outcome of one watch run: which acquisitions are new since last time.

    ``new_items`` are the acquisitions not present in the watch's stored state
    before this run (all of them on the first run, when ``first_run`` is True).
    ``total_seen`` is the size of the state after this run -- the running count
    of distinct acquisitions the watch has ever reported.
    """

    name: str
    new_items: list[UmbraItem]
    first_run: bool
    total_seen: int
    checked_at: str
    query: dict[str, Any]

    @property
    def new_count(self) -> int:
        return len(self.new_items)

    def to_dict(self) -> dict[str, Any]:
        """A machine-readable delta for a scheduler or agent to act on.

        Each new acquisition is rendered as an :meth:`UmbraItem.to_llm_context`
        card (id, datetime, place, bbox, products with URLs), so a downstream
        step can search/download/describe it with no extra fetch. Carries the
        CC-BY attribution, like every data-bearing surface in the library.
        """
        return {
            "watch": self.name,
            "checked_at": self.checked_at,
            "first_run": self.first_run,
            "new_count": self.new_count,
            "total_seen": self.total_seen,
            "query": self.query,
            "new_items": [item.to_llm_context() for item in self.new_items],
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        }


def watch(
    source: SearchSource,
    *,
    name: str,
    store: WatchStore,
    reset: bool = False,
    checked_at: str | None = None,
    **search_kwargs: Any,
) -> WatchResult:
    """Run a search and return only the acquisitions new since the last run.

    Searches ``source`` with ``search_kwargs`` (the same filters as
    :meth:`UmbraCatalog.search` -- ``bbox``, ``area``, ``fuzzy``,
    ``product_types``, ``start``, ``end``, ``limit`` ...), compares the results
    against the set of keys ``store`` has recorded under ``name``, reports the
    difference as new, and folds the new keys back into the store. Idempotent:
    an immediate re-run with no newly published data reports zero new items.

    ``reset=True`` ignores any prior state, so the run re-establishes a baseline
    (everything found is reported as new, ``first_run`` True). ``checked_at``
    overrides the recorded run date (an ISO string); it defaults to today and is
    injectable so tests stay deterministic.
    """
    prior = None if reset else store.load(name)
    first_run = prior is None
    seen: set[str] = set() if prior is None else set(prior)

    new_items: list[UmbraItem] = []
    for item in source.search(**search_kwargs):
        key = _item_key(item)
        if key is None:
            continue
        if key not in seen:
            new_items.append(item)
            seen.add(key)

    store.save(name, seen)
    return WatchResult(
        name=name,
        new_items=new_items,
        first_run=first_run,
        total_seen=len(seen),
        checked_at=checked_at or date.today().isoformat(),
        query=_clean_query(search_kwargs),
    )
