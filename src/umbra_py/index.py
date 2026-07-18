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

import heapq
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ._geometry import Geometry, geometry_bbox
from .catalog import DateLike, UmbraCatalog, _acq_date, _coerce_date
from .constants import CATALOG_INDEX_DB_URL
from .exceptions import IndexSchemaError
from .fuzzy import matching_tasks
from .models import BBox, UmbraItem

#: On-disk schema version, stored via ``PRAGMA user_version``. Bump it whenever
#: the table layout changes (a new column, a new index the queries assume) so an
#: index written by an incompatible umbra-py is detected on open rather than
#: misread. The index is the expensive, *published* artifact every ``--local``
#: path and the prebuilt ``catalog.db`` snapshot depend on, so stamping the
#: version now -- while every deployed database still shares one layout -- is what
#: makes a future migration possible instead of a confusing break.
#:
#: Version 2 added the ``items.place`` column (a baked reverse-geocoded place
#: label; see :meth:`CatalogIndex.bake_places`). It is purely additive, so a
#: version-1 (or legacy version-0) database is migrated in place by adding the
#: column -- the first real exercise of the migration path versioning was landed
#: to enable.
_SCHEMA_VERSION = 2

#: The versions this build knows how to upgrade to :data:`_SCHEMA_VERSION` in
#: place. Every step so far is additive (a new nullable column), handled
#: idempotently by :meth:`CatalogIndex._migrate`; a version not listed here is
#: an older schema with no migration path and is rejected on open.
_MIGRATABLE_FROM = frozenset({0, 1})

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
    doc       TEXT NOT NULL,
    place     TEXT
);
CREATE TABLE IF NOT EXISTS item_assets (
    href  TEXT NOT NULL,
    asset TEXT NOT NULL,
    PRIMARY KEY (href, asset)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_acq_date ON items(acq_date);
CREATE INDEX IF NOT EXISTS idx_items_task ON items(task);
CREATE INDEX IF NOT EXISTS idx_items_id ON items(id);
CREATE INDEX IF NOT EXISTS idx_item_assets_asset ON item_assets(asset);
"""


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of an incremental :meth:`CatalogIndex.update`.

    ``added`` counts acquisitions whose href was not already in the index;
    ``refreshed`` counts those whose existing row was replaced; ``scanned`` is
    their sum (every item the scoped walk yielded). ``start`` is the
    acquisition-date lower bound the walk used -- ``None`` when the index was
    empty and ``update`` fell back to a full build.
    """

    scanned: int
    added: int
    refreshed: int
    start: date | None


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
        self._init_schema()

    def _init_schema(self) -> None:
        """Create or adopt the schema, guarding on the ``PRAGMA user_version``.

        A fresh database reads ``user_version == 0``; so does a legacy database
        written before versioning existed. Both, plus any version listed in
        :data:`_MIGRATABLE_FROM`, are brought up to :data:`_SCHEMA_VERSION` in
        place: the (idempotent) base schema is ensured, additive migrations are
        applied, and the version is stamped. A database written by a *newer*
        umbra-py is unreadable and raises
        :class:`~umbra_py.exceptions.IndexSchemaError` rather than being silently
        misread; a lower version with no migration path raises the same, pointing
        at a rebuild.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            # Same layout; make sure any additive `CREATE ... IF NOT EXISTS`
            # objects are present, then leave the stamp untouched.
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            return
        if version > _SCHEMA_VERSION:
            self._conn.close()
            raise IndexSchemaError(
                f"Catalog index at {self.path} has schema version {version}, but "
                f"this umbra-py supports version {_SCHEMA_VERSION}. Upgrade umbra-py, "
                "or rebuild the index with 'umbra index build' / 'umbra index fetch'."
            )
        if version not in _MIGRATABLE_FROM:
            self._conn.close()
            raise IndexSchemaError(
                f"Catalog index at {self.path} has an older schema version {version} "
                f"(this umbra-py uses version {_SCHEMA_VERSION}) and cannot be "
                "migrated in place. Rebuild it with 'umbra index build' or refetch "
                "with 'umbra index fetch'."
            )
        # A fresh, pre-versioning, or older-but-migratable database: ensure the
        # base schema (which creates everything for a fresh file), apply the
        # additive migrations an existing table might be missing, then stamp.
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self._conn.commit()

    def _migrate(self) -> None:
        """Apply additive, idempotent migrations to reach the current schema.

        Each step is a nullable-column add that ``CREATE TABLE IF NOT EXISTS``
        can't retrofit onto an existing table, so it is applied by checking the
        live column set. Idempotent by construction (a column already present is
        skipped), so running it against a fresh table -- which the base schema
        created complete -- is a no-op.
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(items)")}
        if "place" not in columns:  # v1 -> v2: baked reverse-geocoded place label
            self._conn.execute("ALTER TABLE items ADD COLUMN place TEXT")

    @classmethod
    def from_release(
        cls,
        path: str | os.PathLike | None = None,
        *,
        url: str | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> CatalogIndex:
        """Download the published prebuilt index and open it.

        Umbra has no STAC API, so a fresh install would otherwise crawl the
        whole S3 bucket (minutes) before ``search`` returns anything. This
        fetches the weekly-rebuilt ``catalog.db`` snapshot from the project's
        rolling ``catalog-index`` GitHub release straight to ``path`` (default:
        :func:`default_index_path`) and returns an open index over it, so
        whole-catalog local search works out of the box -- no crawl. Re-run any
        time to refresh; the download is resume-safe and always overwrites the
        existing file. ``url`` overrides the release asset location (e.g. to
        pull from a fork or a mirror).
        """
        from .download import download_url  # local dependency; keep the import cheap

        dest = Path(path) if path is not None else default_index_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        download_url(url or CATALOG_INDEX_DB_URL, dest, overwrite=True, progress=progress)
        return cls(dest)

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

    # -- metadata --------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Record a key/value note about this index (does not commit)."""
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        """Read a metadata note, or ``None`` if it was never set."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # -- writing ---------------------------------------------------------------

    def _has(self, href: str) -> bool:
        """Whether an item with this sidecar href is already indexed."""
        row = self._conn.execute("SELECT 1 FROM items WHERE href = ? LIMIT 1", (href,)).fetchone()
        return row is not None

    def add(self, item: UmbraItem) -> bool:
        """Upsert one item (does not commit). Returns ``False`` (and skips) an
        item with no sidecar href, since the href is the row's identity.

        On a re-index (same href) every STAC-derived column is refreshed, but the
        baked ``place`` label is deliberately left untouched -- it is a derived
        denormalization keyed on the footprint, not on the STAC document, so an
        ``umbra index update`` that re-reads the sidecar must not clear a label an
        ``umbra index bake`` already computed.
        """
        href = item.href
        if not href:
            return False
        bbox: BBox | None = item.bbox
        min_lon, min_lat, max_lon, max_lat = bbox if bbox else (None, None, None, None)
        dt = item.datetime
        acq = _index_acq_date(item)
        self._conn.execute(
            "INSERT INTO items "
            "(href, id, task, datetime, acq_date, min_lon, min_lat, max_lon, max_lat, doc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(href) DO UPDATE SET "
            "id=excluded.id, task=excluded.task, datetime=excluded.datetime, "
            "acq_date=excluded.acq_date, min_lon=excluded.min_lon, min_lat=excluded.min_lat, "
            "max_lon=excluded.max_lon, max_lat=excluded.max_lat, doc=excluded.doc",
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

    def build(
        self,
        catalog: UmbraCatalog | None = None,
        *,
        progress: Callable[[int], None] | None = None,
        **search_kwargs: object,
    ) -> int:
        """Walk the live catalog and persist every matching item.

        Accepts the same keyword filters as :meth:`UmbraCatalog.search`
        (``bbox``, ``start``, ``end``, ``area``, ``product_types``, ``limit``,
        ``max_per_task``) to scope the build. **Pass no filters to index the
        whole bucket** -- the one-time crawl that makes every later
        ``search(local)`` instant. Idempotent: re-running refreshes existing
        rows and adds new ones, so an index can be grown incrementally.

        ``progress``, if given, is called with the running count of items
        written -- a full-bucket build lists every task and takes a while, so
        the CLI uses this to show a live tally. Returns the total written.
        """
        catalog = catalog or UmbraCatalog()
        written = 0
        for item in catalog.search(**search_kwargs):  # type: ignore[arg-type]
            if self.add(item):
                written += 1
                if written % 200 == 0:
                    self._conn.commit()
            if progress is not None:
                progress(written)
        # Stamp the build date so `umbra index info` (and a fetched snapshot)
        # can report staleness -- the acquisition span alone doesn't say when
        # the crawl last ran.
        self.set_meta("built_at", date.today().isoformat())
        self._conn.commit()
        return written

    def update(
        self,
        catalog: UmbraCatalog | None = None,
        *,
        overlap_days: int = 1,
        since: DateLike = None,
        progress: Callable[[int], None] | None = None,
        **search_kwargs: object,
    ) -> UpdateResult:
        """Cheaply refresh the index by re-walking only recent acquisitions.

        A full :meth:`build` fetches a sidecar for *every* acquisition in
        scope; on an index only days old, almost all of that work re-reads
        unchanged data. ``update`` instead derives an acquisition-date lower
        bound from what the index already holds -- the maximum indexed
        ``acq_date`` minus ``overlap_days`` -- and passes it as ``start`` to the
        live walk. The walk prunes older acquisitions' sidecar fetches (see
        :meth:`UmbraCatalog.search`), so a weekly refresh reads only the new
        passes rather than the whole catalog, and every returned row is upserted
        exactly as :meth:`build` does. It is the incremental companion to
        :meth:`from_release`: fetch the weekly snapshot once, then ``update`` to
        catch acquisitions published since.

        The bound is on *acquisition* date, not publish date, so a scene
        acquired before the bound but published after the last build is not
        picked up. ``overlap_days`` (default 1) re-scans a little past the newest
        indexed date to catch the common near-real-time lag; widen it (or run a
        full :meth:`build`) when completeness over back-dated late arrivals
        matters. An empty index has no bound to derive, so ``update`` falls back
        to a full build (``start=None``). Pass ``since`` to force a specific
        lower bound instead of deriving one.

        Extra keyword filters (``bbox``, ``area``, ``product_types``, ``limit``,
        ``max_per_task``) scope the walk exactly as :meth:`build` does -- pass
        the same scope the index was built with. ``start`` may not be passed
        (the bound is what ``update`` computes); use ``since`` to override it.
        Returns an :class:`UpdateResult` tallying new vs. refreshed rows.
        """
        if "start" in search_kwargs:
            raise TypeError(
                "update() derives the acquisition-date bound from the index; "
                "pass 'since=' to override it, not 'start='."
            )
        if since is not None:
            start = _coerce_date(since)
        else:
            max_acq = self._conn.execute("SELECT MAX(acq_date) FROM items").fetchone()[0]
            if max_acq is None:
                start = None  # empty index -> nothing to derive; do a full build
            else:
                start = date.fromisoformat(max_acq) - timedelta(days=max(0, overlap_days))

        catalog = catalog or UmbraCatalog()
        scanned = added = refreshed = 0
        for item in catalog.search(start=start, **search_kwargs):  # type: ignore[arg-type]
            existed = bool(item.href) and self._has(item.href)
            if self.add(item):
                scanned += 1
                if existed:
                    refreshed += 1
                else:
                    added += 1
                if scanned % 200 == 0:
                    self._conn.commit()
            if progress is not None:
                progress(scanned)
        self.set_meta("built_at", date.today().isoformat())
        self._conn.commit()
        return UpdateResult(scanned=scanned, added=added, refreshed=refreshed, start=start)

    def bake_places(
        self,
        geocoder: Callable[[float, float], str | None] | None = None,
        *,
        zoom: int = 10,
        limit: int | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """Reverse-geocode each item's footprint once and cache the place label.

        Reverse geocoding is rate-limited (OpenStreetMap's Nominatim allows one
        request per second) and, until now, ran at *render* time -- so labelling
        a whole catalog in a map or the ``umbra demo`` explorer was impractical.
        This bakes the label in ahead of time: for every indexed acquisition that
        has a footprint but no label yet, it resolves the footprint centroid to a
        human place name (e.g. ``"Reykjavík, Iceland"``) and stores it in the
        ``place`` column, so every later ``search``/``get`` yields it on
        :attr:`UmbraItem.place` for free -- turning the shared index into a
        labelled demo backend.

        It is **idempotent**: only items whose ``place`` is still ``NULL`` are
        geocoded, so a re-run labels just what was added since (and an item whose
        geocode returns nothing is retried on the next run rather than marked).
        ``limit`` caps how many items are geocoded this call (to bake a large
        catalog in bounded batches); ``zoom`` is the Nominatim address
        granularity (3 = country ... 10 = city ... 18 = building). ``progress``,
        if given, is called with the running count of items processed.

        ``geocoder`` is an injectable ``(lat, lon) -> label | None`` callable; the
        default wraps :func:`umbra_py.viz._reverse_geocode`, which self-throttles
        to Nominatim's policy and caches in-process. Passing a stand-in keeps the
        whole path offline-testable. Returns the number of items newly labelled.
        """
        if geocoder is None:
            from .viz import _reverse_geocode  # noqa: PLC0415

            def geocoder(lat: float, lon: float) -> str | None:
                return _reverse_geocode(lat, lon, zoom=zoom)

        rows = self._conn.execute(
            "SELECT href, min_lon, min_lat, max_lon, max_lat FROM items "
            "WHERE place IS NULL AND min_lon IS NOT NULL AND min_lat IS NOT NULL "
            "ORDER BY href"
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]

        labelled = 0
        for processed, (href, min_lon, min_lat, max_lon, max_lat) in enumerate(rows, 1):
            lat = (min_lat + max_lat) / 2.0
            lon = (min_lon + max_lon) / 2.0
            label = geocoder(lat, lon)
            if label:
                self._conn.execute("UPDATE items SET place = ? WHERE href = ?", (label, href))
                labelled += 1
                if labelled % 50 == 0:
                    self._conn.commit()
            if progress is not None:
                progress(processed)
        self._conn.commit()
        return labelled

    # -- querying --------------------------------------------------------------

    def search(
        self,
        *,
        bbox: BBox | None = None,
        intersects: Geometry | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        limit: int | None = None,
        max_per_task: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Yield indexed items matching the filters.

        Same semantics as :meth:`UmbraCatalog.search`, answered from local SQL.
        Only returns acquisitions already present in the index; build or refresh
        it with :meth:`build` first. ``fuzzy=True`` widens ``area`` to the same
        deterministic token-wise match the live path uses
        (:func:`umbra_py.fuzzy.matching_tasks`): the distinct task names are
        read from the index and matched in Python, so both backends agree.

        ``intersects`` (the exterior-ring form from
        :func:`umbra_py._geometry.parse_geometry`) keeps only items whose
        footprint intersects the polygon. Its bounding box is pushed into SQL as
        a cheap prefilter and the exact polygon test then runs in Python on each
        candidate, so the result matches :meth:`UmbraCatalog.search` exactly.
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end, is_end=True)
        conditions: list[str] = []
        params: list[object] = []

        if start_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date >= ?)")
            params.append(start_d.isoformat())
        if end_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date <= ?)")
            params.append(end_d.isoformat())
        if area and fuzzy:
            # SQL LIKE can't express the token-wise fuzzy match, so resolve the
            # matching task names in Python (same matcher as the live path) and
            # constrain to them. An empty match set means nothing can match.
            names = [
                row[0]
                for row in self._conn.execute(
                    "SELECT DISTINCT task FROM items WHERE task IS NOT NULL"
                )
            ]
            matched = matching_tasks(area, names)
            if not matched:
                return
            placeholders = ", ".join("?" * len(matched))
            conditions.append(f"task IN ({placeholders})")
            params += matched
        elif area:
            conditions.append("task IS NOT NULL AND LOWER(task) LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(area.lower())}%")
        # A polygon filter pushes its own bounding box into SQL as a cheap
        # prefilter (the exact polygon test runs in Python on each candidate
        # below); combined with an explicit ``bbox`` both boxes must overlap.
        boxes = [b for b in (bbox, geometry_bbox(intersects) if intersects else None) if b]
        for box in boxes:
            # Footprint bbox overlaps the query bbox (matches
            # UmbraItem.intersects_bbox); items with no bbox never match.
            conditions.append(
                "min_lon IS NOT NULL AND max_lon >= ? AND min_lon <= ? "
                "AND max_lat >= ? AND min_lat <= ?"
            )
            params += [box[0], box[2], box[1], box[3]]
        if product_types:
            wanted = [p.upper() for p in product_types]
            placeholders = ", ".join("?" * len(wanted))
            conditions.append(
                f"href IN (SELECT href FROM item_assets WHERE asset IN ({placeholders}))"
            )
            params += wanted

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            f"SELECT href, doc, place FROM items{where} ORDER BY task IS NULL, task, acq_date, href"
        )

        count = 0
        per_task: dict[str | None, int] = {}
        for href, doc, place in self._conn.execute(sql, params):
            item = UmbraItem.from_dict(json.loads(doc), href=href)
            item.place = place
            if intersects is not None and not item.intersects_polygon(intersects):
                continue
            if max_per_task is not None:
                seen = per_task.get(item.task, 0)
                if seen >= max_per_task:
                    continue
                per_task[item.task] = seen + 1
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    def search_live(
        self,
        catalog: UmbraCatalog | None = None,
        *,
        overlap_days: int = 1,
        refresh: bool = True,
        bbox: BBox | None = None,
        intersects: Geometry | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        limit: int | None = None,
        max_per_task: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Read-through search: the index for the bulk, a live delta for what's new.

        :meth:`search` is instant but only returns what the index already holds;
        :meth:`UmbraCatalog.search` is always current but re-walks the whole
        bucket every call. This is the transparent middle the analysis doc names
        as "make the index the default path" (``docs/CODEBASE_ANALYSIS.md``
        §4.4): the index answers the whole query from local SQL, and a *bounded*
        live walk covers only acquisitions at or after the index's freshness
        horizon -- its maximum indexed ``acq_date`` minus ``overlap_days`` -- so
        the walk fetches sidecars only for recent passes rather than the whole
        catalog (the same pruning :meth:`update` relies on). The two streams are
        merged in the usual ``(task, acq_date)`` order and de-duplicated by
        sidecar href, so an acquisition the index already knows is never yielded
        twice; the result is what a single fresh search would return, without
        paying for a full crawl.

        With ``refresh=True`` (the default) each genuinely new acquisition the
        live delta discovers is upserted into the index as it is yielded -- the
        "read-through cache warms" behavior -- so the next call needs an even
        smaller (often empty) walk. Set ``refresh=False`` to leave the index
        untouched (e.g. when it is a shared read-only snapshot); a read-only
        database also disables warming automatically rather than failing the
        search. The write-back is committed only when at least one new row was
        added, and ``built_at`` is re-stamped then, exactly as :meth:`update`.

        The keyword filters (``bbox``, ``start``, ``end``, ``product_types``,
        ``area``, ``fuzzy``, ``limit``, ``max_per_task``) mean exactly what they
        do on :meth:`search` / :meth:`UmbraCatalog.search`; ``start`` bounds both
        streams (the live delta never walks older than the caller asked for, even
        when the freshness horizon is older). ``overlap_days`` (default 1)
        re-scans a little past the newest indexed date to catch near-real-time
        publish lag; the bound is on *acquisition* date, so a back-dated late
        arrival still wants a widened overlap or a full :meth:`build`. An empty
        index has no horizon, so the live walk covers the caller's full window
        (and, with ``refresh``, this doubles as a first :meth:`build`).
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end, is_end=True)

        # Freshness horizon: the newest acquisition the index already knows. The
        # live walk only needs to cover from there forward (minus the overlap),
        # but never older than the caller's own start bound.
        max_acq = self._conn.execute("SELECT MAX(acq_date) FROM items").fetchone()[0]
        if max_acq is not None:
            delta_start: date | None = date.fromisoformat(max_acq) - timedelta(
                days=max(0, overlap_days)
            )
            if start_d is not None and start_d > delta_start:
                delta_start = start_d
        else:
            # Empty index: nothing to prune against, so walk the caller's window.
            delta_start = start_d

        filters: dict[str, object] = {
            "bbox": bbox,
            "intersects": intersects,
            "end": end_d,
            "product_types": product_types,
            "area": area,
            "fuzzy": fuzzy,
        }
        index_stream = self.search(start=start_d, **filters)  # type: ignore[arg-type]
        catalog = catalog or UmbraCatalog()
        live_stream = catalog.search(start=delta_start, **filters)  # type: ignore[arg-type]

        def keyed(items: Iterator[UmbraItem], origin: str):
            for it in items:
                acq = _index_acq_date(it)
                key = (
                    0 if it.task is None else 1,
                    it.task or "",
                    acq.isoformat() if acq else "",
                    it.href or "",
                )
                yield key, origin, it

        merged = heapq.merge(
            keyed(index_stream, "index"),
            keyed(live_stream, "live"),
            key=lambda t: t[0],
        )

        seen: set[str] = set()
        warm = refresh
        added_any = False
        count = 0
        per_task: dict[str | None, int] = {}
        try:
            for _key, origin, item in merged:
                href = item.href
                if href and href in seen:
                    continue  # already emitted from the other stream
                if origin == "live" and warm and not (href and self._has(href)):
                    try:
                        if self.add(item):
                            added_any = True
                    except sqlite3.OperationalError:
                        warm = False  # read-only index: leave it, results still correct
                if href:
                    seen.add(href)
                if max_per_task is not None:
                    n = per_task.get(item.task, 0)
                    if n >= max_per_task:
                        continue
                    per_task[item.task] = n + 1
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
        finally:
            if added_any:
                self.set_meta("built_at", date.today().isoformat())
                self.commit()

    def get(self, item_id: str) -> UmbraItem | None:
        """Return the indexed item with this STAC id, or ``None`` if absent.

        The keyed point-lookup complement to :meth:`search`'s listing: where
        filtering a full ``search`` by id would scan the ordered result set,
        this is an ``idx_items_id``-backed lookup, so it stays fast as the
        published ``catalog.db`` snapshot grows. STAC ids are unique per
        acquisition in Umbra's catalog; in the unlikely event two sidecars
        share an id, the first by ``href`` order is returned deterministically.
        """
        row = self._conn.execute(
            "SELECT href, doc, place FROM items WHERE id = ? ORDER BY href LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        href, doc, place = row
        item = UmbraItem.from_dict(json.loads(doc), href=href)
        item.place = place
        return item

    def stats(self) -> dict[str, object]:
        """Summary counts for ``umbra index info``: item count, acquisition-date
        span, number of distinct tasks, how many items carry a baked place label
        (``labeled``; see :meth:`bake_places`), and the date the index was last
        built (``built_at``, ``None`` for an index written before build
        stamping)."""
        items, start, end, tasks, labeled = self._conn.execute(
            "SELECT COUNT(*), MIN(acq_date), MAX(acq_date), COUNT(DISTINCT task), "
            "COUNT(place) FROM items"
        ).fetchone()
        return {
            "items": items,
            "start": start,
            "end": end,
            "tasks": tasks,
            "labeled": labeled,
            "built_at": self.get_meta("built_at"),
        }
