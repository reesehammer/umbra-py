"""A local SQLite index of Umbra acquisitions for fast, repeatable search.

Umbra publishes no STAC API, so :class:`umbra_py.UmbraCatalog` answers every
search by re-walking the public S3 bucket -- paginated LIST requests plus a
sidecar GET per acquisition (see ``catalog.py``). That walk is network-bound
and identical across repeat searches.

:class:`CatalogIndex` persists the items a walk discovers into a local SQLite
database and answers searches from SQL instead, so a repeat (or overlapping)
search is a local query rather than a fresh crawl. It is deliberately a
first-class, reusable building block -- the substrate for a shared, prebuilt
catalog (walk once, ship the ``.db``) or a service layered on top of this
library -- not just an internal cache. Its :meth:`~CatalogIndex.search`
mirrors :meth:`UmbraCatalog.search`, so callers can swap the live walk for a
local query without changing anything else.

Each acquisition is one row, keyed by its sidecar URL (unique within the
bucket), carrying the columns the search filters need (acquisition date,
bounding box, task, product assets) plus the full reconstructed STAC item JSON
so an :class:`~umbra_py.UmbraItem` rebuilds without another network round trip.
Re-indexing an acquisition replaces its row, so :meth:`~CatalogIndex.build` is
an idempotent upsert and an index can be grown incrementally.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from .catalog import DateLike, UmbraCatalog, _acq_date, _coerce_date
from .models import BBox, UmbraItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    href      TEXT PRIMARY KEY,
    id        TEXT NOT NULL,
    task      TEXT,
    datetime  TEXT,
    acq_date  TEXT,
    min_lon   REAL,
    min_lat   REAL,
    max_lon   REAL,
    max_lat   REAL,
    doc       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_assets (
    href  TEXT NOT NULL,
    asset TEXT NOT NULL,
    PRIMARY KEY (href, asset)
);
CREATE INDEX IF NOT EXISTS idx_items_acq_date ON items(acq_date);
CREATE INDEX IF NOT EXISTS idx_items_task ON items(task);
CREATE INDEX IF NOT EXISTS idx_item_assets_asset ON item_assets(asset);
"""


def default_index_path() -> Path:
    """Where the index lives by default.

    ``$UMBRA_INDEX_DB`` overrides everything; otherwise it sits under the XDG
    cache dir (``$XDG_CACHE_HOME`` or ``~/.cache``) at
    ``umbra-py/catalog.db``.
    """
    override = os.environ.get("UMBRA_INDEX_DB")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "umbra-py" / "catalog.db"


def _index_acq_date(item: UmbraItem) -> date | None:
    """Acquisition date to prune on.

    Prefer the acquisition-directory date embedded in the item's sidecar href
    (``.../<YYYY-MM-DD-...>/<...>.stac.v2.json``) -- this is exactly what the
    live walk prunes on -- and fall back to the sidecar ``datetime``.
    """
    href = item.href or ""
    segs = href.rstrip("/").rsplit("/", 2)
    if len(segs) >= 2:
        d = _acq_date(segs[-2])
        if d is not None:
            return d
    dt = item.datetime
    return dt.date() if dt else None


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so an ``area`` substring matches literally.

    Task names contain underscores (e.g. ``Atmospheric-River_Nov-2025``), and
    ``_`` is a single-character LIKE wildcard, so an unescaped match would be
    looser than the live walk's plain ``in`` substring test.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CatalogIndex:
    """A local SQLite index of Umbra acquisitions.

    Open (creating the database and schema if needed) with a path, or no path
    to use :func:`default_index_path`. Usable as a context manager, which
    commits and closes on exit::

        with CatalogIndex() as index:
            index.build(area="centerfield")          # walk S3 once, persist
            for item in index.search(area="centerfield"):  # local, instant
                print(item.summary())
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path is not None else default_index_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle -------------------------------------------------------------

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    def __enter__(self) -> CatalogIndex:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # -- writing ---------------------------------------------------------------

    def add(self, item: UmbraItem) -> bool:
        """Upsert one item (does not commit). Returns ``False`` (and skips) an
        item with no sidecar href, since the href is the row's identity."""
        href = item.href
        if not href:
            return False
        bbox: BBox | None = item.bbox
        min_lon, min_lat, max_lon, max_lat = bbox if bbox else (None, None, None, None)
        dt = item.datetime
        acq = _index_acq_date(item)
        self._conn.execute(
            "INSERT OR REPLACE INTO items "
            "(href, id, task, datetime, acq_date, min_lon, min_lat, max_lon, max_lat, doc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                href,
                item.id,
                item.task,
                dt.isoformat() if dt else None,
                acq.isoformat() if acq else None,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                json.dumps(item.raw),
            ),
        )
        self._conn.execute("DELETE FROM item_assets WHERE href = ?", (href,))
        self._conn.executemany(
            "INSERT OR IGNORE INTO item_assets (href, asset) VALUES (?, ?)",
            [(href, asset) for asset in item.available_assets],
        )
        return True

    def build(self, catalog: UmbraCatalog | None = None, **search_kwargs: object) -> int:
        """Walk the live catalog and persist every matching item.

        Accepts the same keyword filters as :meth:`UmbraCatalog.search`
        (``bbox``, ``start``, ``end``, ``area``, ``product_types``, ``limit``,
        ``max_per_task``) to scope the build, or none to index the whole
        bucket. Idempotent -- re-running refreshes existing rows and adds new
        ones -- so an index can be grown incrementally. Returns the number of
        acquisitions written.
        """
        catalog = catalog or UmbraCatalog()
        written = 0
        for item in catalog.search(**search_kwargs):  # type: ignore[arg-type]
            if self.add(item):
                written += 1
                if written % 200 == 0:
                    self._conn.commit()
        self._conn.commit()
        return written

    # -- querying --------------------------------------------------------------

    def search(
        self,
        *,
        bbox: BBox | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        area: str | None = None,
        limit: int | None = None,
        max_per_task: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Yield indexed items matching the filters.

        Same semantics as :meth:`UmbraCatalog.search`, answered from local SQL.
        Only returns acquisitions already present in the index; build or refresh
        it with :meth:`build` first.
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end)
        conditions: list[str] = []
        params: list[object] = []

        if start_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date >= ?)")
            params.append(start_d.isoformat())
        if end_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date <= ?)")
            params.append(end_d.isoformat())
        if area:
            conditions.append("task IS NOT NULL AND LOWER(task) LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(area.lower())}%")
        if bbox is not None:
            # Footprint bbox overlaps the query bbox (matches
            # UmbraItem.intersects_bbox); items with no bbox never match.
            conditions.append(
                "min_lon IS NOT NULL AND max_lon >= ? AND min_lon <= ? "
                "AND max_lat >= ? AND min_lat <= ?"
            )
            params += [bbox[0], bbox[2], bbox[1], bbox[3]]
        if product_types:
            wanted = [p.upper() for p in product_types]
            placeholders = ", ".join("?" * len(wanted))
            conditions.append(
                f"href IN (SELECT href FROM item_assets WHERE asset IN ({placeholders}))"
            )
            params += wanted

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT href, doc FROM items{where} ORDER BY task IS NULL, task, acq_date, href"

        count = 0
        per_task: dict[str | None, int] = {}
        for href, doc in self._conn.execute(sql, params):
            item = UmbraItem.from_dict(json.loads(doc), href=href)
            if max_per_task is not None:
                seen = per_task.get(item.task, 0)
                if seen >= max_per_task:
                    continue
                per_task[item.task] = seen + 1
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    def stats(self) -> dict[str, object]:
        """Summary counts for ``umbra index info``: item count, acquisition-date
        span, and number of distinct tasks."""
        items, start, end, tasks = self._conn.execute(
            "SELECT COUNT(*), MIN(acq_date), MAX(acq_date), COUNT(DISTINCT task) FROM items"
        ).fetchone()
        return {"items": items, "start": start, "end": end, "tasks": tasks}
