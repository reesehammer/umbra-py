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
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._geometry import Geometry, geometry_bbox
from ._http import default_session
from .catalog import DateLike, UmbraCatalog, _acq_date, _coerce_date
from .constants import CATALOG_INDEX_DB_URL
from .exceptions import IndexRefreshError, IndexSchemaError, InsufficientSpaceError
from .fuzzy import matching_tasks
from .models import BBox, UmbraItem

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .coverage import SiteCoverage

#: On-disk schema version, stored via ``PRAGMA user_version``. Bump it whenever
#: the table layout changes (a new column, a new index the queries assume) so an
#: index written by an incompatible umbra-py is detected on open rather than
#: misread. The index is the expensive, *published* artifact every ``--local``
#: path and the prebuilt ``catalog.db`` snapshot depend on, so stamping the
#: version now -- while every deployed database still shares one layout -- is what
#: makes a future migration possible instead of a confusing break.
#:
#: Version 2 added the ``items.place`` column (a baked reverse-geocoded place
#: label; see :meth:`CatalogIndex.bake_places`). Version 3 added the
#: ``items.thumbnail`` column (a baked SAR quicklook PNG; see
#: :meth:`CatalogIndex.bake_thumbnails`). Version 4 added
#: ``items.thumbnail_asset`` / ``items.thumbnail_size`` -- *what* that PNG is a
#: picture of, which the index used to leave to be assumed. All are purely
#: additive, so a lower-version (or legacy version-0) database is migrated in
#: place by adding the missing columns -- exercising the migration path
#: versioning was landed to enable.
_SCHEMA_VERSION = 4

#: Longest edge of the published weekly ``catalog.thumbs.db`` bake, and the
#: default for :meth:`CatalogIndex.bake_thumbnails`. 512 px is large enough
#: to read a scene in an MCP client; a full-resolution render still wants a
#: local COG stream (typically 1024 px).
PUBLISHED_THUMBNAIL_SIZE = 512

#: How long (milliseconds) a connection waits for a lock held by another
#: connection before raising ``sqlite3.OperationalError: database is locked``.
#: The index is now a *shared* artifact -- a running ``umbra serve`` (or a demo,
#: or the MCP server) reads it while a CLI writer (``umbra index update`` /
#: ``build`` / ``bake-*``, or a ``search_live`` refresh) holds a write
#: transaction -- so a reader that arrives mid-write should wait a moment rather
#: than fail immediately. Paired with WAL journal mode (see
#: :meth:`CatalogIndex._configure_connection`), which lets readers proceed
#: without blocking on the writer at all, this makes concurrent multi-process
#: access the norm rather than a race.
_BUSY_TIMEOUT_MS = 5000

#: The versions this build knows how to upgrade to :data:`_SCHEMA_VERSION` in
#: place. Every step so far is additive (a new nullable column), handled
#: idempotently by :meth:`CatalogIndex._migrate`; a version not listed here is
#: an older schema with no migration path and is rejected on open.
_MIGRATABLE_FROM = frozenset({0, 1, 2, 3})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    href            TEXT PRIMARY KEY,
    id              TEXT NOT NULL,
    task            TEXT,
    datetime        TEXT,
    acq_date        TEXT,
    min_lon         REAL,
    min_lat         REAL,
    max_lon         REAL,
    max_lat         REAL,
    doc             TEXT NOT NULL,
    place           TEXT,
    thumbnail       BLOB,
    thumbnail_asset TEXT,
    thumbnail_size  INTEGER
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


#: Schema of the *thumbnail sidecar* -- the transportable half of the baked
#: previews (see :meth:`CatalogIndex.export_thumbnails`). It is a separate file
#: rather than a column of the published ``catalog.db`` on purpose: a PNG per
#: acquisition dwarfs the metadata it hangs off, and every ``umbra index fetch``
#: would then pay for pixels most callers never look at. Keyed by ``href`` (the
#: index's own primary key, so a merge back is exact) and carrying the STAC
#: ``id`` beside it, so the file is also readable on its own. ``asset`` and
#: ``size`` say what each PNG is a picture of, which is what lets a merge prefer
#: the larger preview of a product over the one that happened to arrive first
#: (see :meth:`CatalogIndex.import_thumbnails`); a sidecar written before they
#: existed simply lacks the columns and is read without them.
_THUMBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbnails (
    href  TEXT PRIMARY KEY,
    id    TEXT NOT NULL,
    png   BLOB NOT NULL,
    asset TEXT,
    size  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_thumbnails_id ON thumbnails(id);
"""

#: The sidecar columns :data:`_THUMBS_SCHEMA` gained after the first published
#: ``catalog.thumbs.db``. ``CREATE TABLE IF NOT EXISTS`` cannot retrofit them
#: onto a file that already exists, so both sides check the live column set --
#: :meth:`CatalogIndex.export_thumbnails` adds what is missing before writing,
#: :meth:`CatalogIndex.import_thumbnails` selects only what is there.
_THUMBS_PROVENANCE_COLUMNS = {"asset": "TEXT", "size": "INTEGER"}

#: The default merge rule of :meth:`CatalogIndex.import_thumbnails`: fill a gap,
#: and otherwise keep the local bake unless the incoming one is a bigger preview
#: of the same product. SQL's three-valued logic carries the "only when both say
#: what they are" half for free -- a comparison against an unrecorded size is
#: ``NULL``, so it does not replace anything -- while ``IS`` compares the assets
#: without tripping over the same ``NULL``.
_KEEP_UNLESS_LARGER = " AND (thumbnail IS NULL OR (? > thumbnail_size AND thumbnail_asset IS ?))"


def _widen_thumbs_sidecar(conn: sqlite3.Connection) -> None:
    """Add any :data:`_THUMBS_PROVENANCE_COLUMNS` an existing sidecar lacks.

    The index's own :meth:`CatalogIndex._migrate` for the transportable half:
    ``CREATE TABLE IF NOT EXISTS`` leaves a file written by an older umbra-py
    alone, so exporting into one would otherwise fail on the new columns.
    Idempotent, and a no-op on the fresh file the schema just created complete.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(thumbnails)")}
    for name, sql_type in _THUMBS_PROVENANCE_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE thumbnails ADD COLUMN {name} {sql_type}")


#: Grid cell, in degrees, used to group one *site's* acquisitions when baking
#: place labels with ``by_site=True``. Umbra files every pass over a site under
#: a single task directory and a pass's footprint is a few km across, so a
#: site's passes land in the same ~11 km cell and are geocoded once. A task
#: whose passes straddle a cell boundary simply costs an extra geocode call --
#: the failure direction is a redundant lookup, never a mislabelled item.
_SITE_CELL_DEGREES = 0.1


def _site_groups(
    rows: Iterable[tuple[str, str | None, float, float, float, float]],
    by_site: bool,
) -> list[tuple[tuple[float, float], list[str]]]:
    """Group unlabelled index rows into the geocode calls a bake will make.

    Each returned entry is ``((lat, lon), hrefs)``: the centroid to reverse
    geocode, and the acquisitions that take the resulting label. With
    ``by_site`` false every acquisition is its own group (one call per item,
    the original behaviour); with it true, acquisitions sharing a task *and* a
    :data:`_SITE_CELL_DEGREES` cell collapse into one group whose centroid is
    the mean of its members'. Insertion order follows the row order, so the
    grouping -- and therefore a ``limit``-ed batch -- is deterministic.
    """
    groups: dict[object, list[tuple[float, float, str]]] = {}
    for href, task, min_lon, min_lat, max_lon, max_lat in rows:
        lat = (min_lat + max_lat) / 2.0
        lon = (min_lon + max_lon) / 2.0
        key: object = (
            (task, round(lat / _SITE_CELL_DEGREES), round(lon / _SITE_CELL_DEGREES))
            if by_site
            else href
        )
        groups.setdefault(key, []).append((lat, lon, href))
    out = []
    for members in groups.values():
        lat = sum(m[0] for m in members) / len(members)
        lon = sum(m[1] for m in members) / len(members)
        out.append(((lat, lon), [m[2] for m in members]))
    return out


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


@dataclass(frozen=True)
class BakedPreview:
    """A cached quicklook and what it is a picture of.

    :meth:`CatalogIndex.get_thumbnail` returns bytes, which is all a gallery tile
    or a ``GET /artifacts/thumbnail/{id}.png`` needs. A reader that has to decide
    whether the cached picture answers *its* request needs more than the pixels:
    ``asset`` is the product the bake rendered from and ``max_size`` the longest
    edge it asked for, both recorded by :meth:`CatalogIndex.bake_thumbnails`.

    Either may be ``None`` -- a preview baked (or published) before the index
    recorded them. That is "unknown", not "GEC": a consumer should fall back to
    whatever it may safely assume rather than read the absence as a claim.
    """

    png: bytes
    asset: str | None = None
    max_size: int | None = None


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


def default_thumbs_path(index_path: str | os.PathLike | None = None) -> Path:
    """Where the baked-thumbnail sidecar lives by default.

    It sits *beside* the index (``catalog.db`` -> ``catalog.thumbs.db``) so the
    two travel together while staying separate files: the pixels are opt-in and
    an order of magnitude larger than the metadata, so keeping them out of
    ``catalog.db`` is what lets the published index stay small (the same split
    :func:`umbra_py.embed.default_scene_embed_path` makes for vectors). Pass
    ``index_path`` to derive the sibling name from a non-default index location.
    """
    base = Path(index_path) if index_path is not None else default_index_path()
    return base.with_name(f"{base.stem}.thumbs.db")


def fetch_prebuilt_thumbnails(
    dest: str | os.PathLike | None = None,
    *,
    url: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download the published baked-thumbnail sidecar.

    The weekly index workflow bakes a quicklook per acquisition and ships it as
    ``catalog.thumbs.db`` on the rolling ``catalog-index`` release beside
    ``catalog.db`` / ``catalog.pmtiles``, so a fresh install gets scene previews
    without streaming a cloud-optimized GeoTIFF overview per acquisition -- the
    thumbnail sibling of :meth:`CatalogIndex.from_release` and
    :func:`umbra_py.pmtiles.fetch_prebuilt_pmtiles`. This fetches the sidecar to
    ``dest`` (default: :func:`default_thumbs_path`) and returns its path;
    :meth:`CatalogIndex.import_thumbnails` merges it into a local index. Re-run
    any time to refresh; the download is resume-safe and always overwrites the
    existing file. ``url`` overrides the release asset location (e.g. a fork).
    """
    from .constants import CATALOG_INDEX_THUMBS_URL  # noqa: PLC0415
    from .download import download_url  # local dependency; keep the import cheap  # noqa: PLC0415

    target = Path(dest) if dest is not None else default_thumbs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    download_url(url or CATALOG_INDEX_THUMBS_URL, target, overwrite=True, progress=progress)
    return target


def snapshot_state_path(index_path: str | os.PathLike | None = None) -> Path:
    """Where :func:`refresh_from_release` records which snapshot an index is.

    A small JSON file beside the index (``catalog.db`` ->
    ``catalog.db.source.json``) holding the release asset's ``ETag`` and
    ``Last-Modified``, so a restart can tell "same snapshot" from "new weekly
    rebuild" with one ``HEAD`` instead of re-downloading 90 MB.
    """
    base = Path(index_path) if index_path is not None else default_index_path()
    return base.with_name(f"{base.name}.source.json")


def _normalize_etag(value: str | None) -> str | None:
    """An ``ETag`` without its weak ``W/`` prefix or surrounding quotes.

    The header arrives as ``"0x8DE..."`` (or ``W/"..."``); the state file and
    ``/healthz`` carry the bare value. State written before normalization kept
    the quotes, so both sides of a comparison go through here.
    """
    if value is None:
        return None
    value = value.strip()
    if value[:2] in ("W/", "w/"):
        value = value[2:]
    return value.strip('"') or None


def read_snapshot_state(index_path: str | os.PathLike | None = None) -> dict[str, str] | None:
    """The recorded snapshot identity of an index, or ``None`` if never recorded.

    Works for any file :func:`snapshot_state_path` can name, including the
    thumbnail sidecar. The ``etag`` comes back normalized (no quotes).
    """
    path = snapshot_state_path(index_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("etag"), str):
        data["etag"] = _normalize_etag(data["etag"])
    return data


def snapshot_id(state: dict[str, Any] | None) -> str | None:
    """The single value that names a recorded snapshot (``ETag``, else ``Last-Modified``)."""
    if not state:
        return None
    return state.get("etag") or state.get("last_modified")


def _head_snapshot(sess: Any, src: str) -> tuple[str | None, str | None, int | None]:
    """``HEAD`` a release asset; return its normalized ``ETag``, ``Last-Modified``
    and ``Content-Length`` (``None`` when absent or malformed)."""
    try:
        head = sess.head(src, allow_redirects=True, timeout=30)
        head.raise_for_status()
    except Exception as exc:  # requests.RequestException and friends
        raise IndexRefreshError(f"Could not reach the published snapshot {src!r}: {exc}") from exc
    try:
        length: int | None = int(head.headers["Content-Length"])
    except (KeyError, TypeError, ValueError):
        length = None
    return _normalize_etag(head.headers.get("ETag")), head.headers.get("Last-Modified"), length


def _same_snapshot(
    state: dict[str, Any] | None, src: str, etag: str | None, last_modified: str | None
) -> bool:
    return (
        state is not None
        and state.get("url") == src
        and (etag or last_modified) is not None
        and state.get("etag") == etag
        and state.get("last_modified") == last_modified
    )


def _write_snapshot_state(dest: Path, fields: dict[str, Any]) -> None:
    state_path = snapshot_state_path(dest)
    tmp = state_path.with_name(state_path.name + ".tmp")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp.write_text(json.dumps({**fields, "fetched_at": fetched_at}), encoding="utf-8")
    os.replace(tmp, state_path)


@dataclass
class RefreshResult:
    """Outcome of :func:`refresh_from_release`."""

    path: Path
    changed: bool
    etag: str | None
    last_modified: str | None
    items: int | None
    built_at: str | None
    reason: str
    #: Stale scratch files an earlier refresh left behind and this one removed
    #: before downloading (file name -> bytes freed).
    cleaned: dict[str, int] = field(default_factory=dict)


def _validate_snapshot(path: Path) -> tuple[int, str | None]:
    """Open a downloaded snapshot and prove it is a usable index.

    Raises :class:`IndexRefreshError` on a corrupt file, a schema this build
    cannot read, or an empty items table. Returns ``(rows, built_at)``.
    """
    try:
        conn = sqlite3.connect(str(path))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise IndexRefreshError(
            f"Downloaded snapshot {path} is not a readable index: {exc}"
        ) from exc
    if check != "ok":
        raise IndexRefreshError(f"Downloaded snapshot {path} failed quick_check: {check}")
    if version > _SCHEMA_VERSION:
        raise IndexRefreshError(
            f"Downloaded snapshot has schema version {version}; this umbra-py reads "
            f"up to {_SCHEMA_VERSION}. Upgrade umbra-py before refreshing."
        )
    if rows <= 0:
        raise IndexRefreshError(f"Downloaded snapshot {path} has no items.")
    return rows, (row[0] if row else None)


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        side = path.with_name(path.name + suffix)
        try:
            side.unlink()
        except FileNotFoundError:
            pass


#: Free space every refresh step leaves on the volume after its own estimate,
#: so a step that fits exactly cannot leave the serving index unable to write
#: its ``-shm`` / ``-wal`` (the ``disk I/O error`` a full volume produces).
_FREE_SPACE_RESERVE = 64 * 1024 * 1024

#: Slack on the merge estimate. The copy grows by the PNG bytes it writes;
#: each blob's partly filled last overflow page and the ``meta`` write add a
#: little on top.
_MERGE_HEADROOM = 0.10


def _mb(n: int) -> str:
    return f"{n / 1e6:,.0f} MB"


def _require_free_space(directory: Path, need: int, step: str) -> None:
    """Raise :class:`InsufficientSpaceError` unless ``directory`` has ``need`` bytes
    plus :data:`_FREE_SPACE_RESERVE` free."""
    free = shutil.disk_usage(directory).free
    if free < need + _FREE_SPACE_RESERVE:
        raise InsufficientSpaceError(
            f"Not enough free space in {directory} to {step}: needs ~{_mb(need)} "
            f"plus a {_mb(_FREE_SPACE_RESERVE)} reserve, {_mb(free)} free. "
            "Skipped; the current files were kept.",
            hint="Free space in that directory or grow the volume "
            "(docs/deploy.md, 'Volume sizing').",
        )


def _db_bytes(path: Path) -> int:
    """A database's footprint: the file plus any write-ahead log beside it."""
    wal = path.with_name(path.name + "-wal")
    return path.stat().st_size + (wal.stat().st_size if wal.exists() else 0)


@contextmanager
def _refresh_lock(path: Path) -> Iterator[None]:
    """Hold ``<path>.lock`` for one refresh of ``path``.

    Every scratch file :func:`_clean_leftovers` removes is written only under
    this lock, so a cleanup can never delete a download or merge another
    process is still writing. A second refresh of the same file raises instead
    of waiting. Platforms without ``fcntl`` (Windows) take no lock; there, an
    open file cannot be deleted anyway.
    """
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IndexRefreshError(
                f"Another refresh of {path} is in progress (holds {lock_path}); leaving it alone."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _leftover_paths(path: Path) -> list[Path]:
    """The scratch files a refresh or merge of ``path`` writes beside it.

    Only names this module creates under :func:`_refresh_lock`: the staged
    download (``.next``) and its resume sidecars, the merge copy (``.merge``),
    the journals SQLite may leave beside either, and the state file's ``.tmp``.
    Never ``path`` itself nor its own ``-wal`` / ``-shm`` / ``-journal``, which
    belong to whoever has the live database open.
    """
    name = path.name
    names = [f"{name}.next.part", f"{name}.next.part.etag", f"{name}.source.json.tmp"]
    for staged in (f"{name}.next", f"{name}.merge"):
        names += [staged, f"{staged}-wal", f"{staged}-shm", f"{staged}-journal"]
    return [path.with_name(name) for name in names]


def _clean_leftovers(path: Path) -> dict[str, int]:
    """Remove what an interrupted refresh of ``path`` left behind; call under the lock.

    A download killed mid-stream leaves ``.next.part`` (the full asset size for
    the thumbnail sidecar), a merge that hit a full disk leaves its ``.merge``
    copy, and neither is ever resumed by a later boot -- yet both count against
    the same volume the next refresh needs. The live database's own write-ahead
    log is not deleted (it may hold committed pages) but checkpointed through
    SQLite, which folds committed frames in and truncates the file; a failed
    merge's uncommitted tail is simply discarded. Returns name -> bytes freed.
    """
    removed: dict[str, int] = {}
    for leftover in _leftover_paths(path):
        try:
            size = leftover.stat().st_size
            leftover.unlink()
        except FileNotFoundError:
            continue
        removed[leftover.name] = size
    wal = path.with_name(path.name + "-wal")
    if path.exists() and wal.exists() and wal.stat().st_size > 0:
        before = wal.stat().st_size
        try:
            conn = sqlite3.connect(str(path))
            try:
                conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            pass  # a busy or unreadable index is the validation step's problem, not cleanup's
        after = wal.stat().st_size if wal.exists() else 0
        if after < before:
            removed[wal.name] = before - after
    return removed


def _index_rows(path: Path) -> int | None:
    """``COUNT(*)`` of an index's items, or ``None`` when it cannot be read at all."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None


def refresh_from_release(
    path: str | os.PathLike | None = None,
    *,
    url: str | None = None,
    force: bool = False,
    min_ratio: float = 0.5,
    session: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> RefreshResult:
    """Replace the index with the published snapshot only when it changed.

    ``HEAD`` the release asset (following GitHub's redirect) and compare its
    ``ETag`` / ``Last-Modified`` with what :func:`snapshot_state_path` recorded
    for this index. Unchanged: return without downloading. Changed (or no index
    yet, or ``force``): download beside the index to ``<name>.next``, prove it
    is a readable, non-empty index of a schema this build understands, refuse
    it if it has fewer than ``min_ratio`` of the current index's rows (a
    truncated or broken rebuild should not silently shrink a serving index),
    then swap it in with an atomic ``os.replace`` and record the new identity.

    An unchanged snapshot is still re-downloaded when the current file cannot
    be read at all (reason ``"unreadable"``), so a volume that once filled up
    and damaged the index heals on the next boot instead of serving errors.

    Disk safety: first remove what an interrupted earlier refresh left beside
    the index (:func:`_clean_leftovers`), then check the directory has room for
    the asset's ``Content-Length`` before downloading; if not, raise
    :class:`InsufficientSpaceError` and keep the current index. The swap
    itself is a same-directory rename and needs no further space.

    The swap removes the old file's ``-wal`` / ``-shm`` so the new database is
    never paired with a stale write-ahead log. That makes it safe only while no
    process holds the index open, which is why the container entrypoint runs it
    *before* starting the server. Raises :class:`IndexRefreshError` (old index
    untouched) when the download fails validation.
    """
    from .download import download_url  # noqa: PLC0415

    sess = session or default_session()
    dest = Path(path) if path is not None else default_index_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = url or CATALOG_INDEX_DB_URL
    with _refresh_lock(dest):
        cleaned = _clean_leftovers(dest)
        etag, last_modified, length = _head_snapshot(sess, src)
        state = read_snapshot_state(dest)
        existed = dest.exists()
        current = _index_rows(dest) if existed else None
        if existed and _same_snapshot(state, src, etag, last_modified) and not force:
            if current is not None:
                return RefreshResult(
                    dest, False, etag, last_modified, None, None, "unchanged", cleaned
                )

        _require_free_space(dest.parent, length or 0, f"download {src.rsplit('/', 1)[-1]}")
        staging = dest.with_name(dest.name + ".next")
        try:
            download_url(src, staging, overwrite=True, session=sess, progress=progress)
            rows, built_at = _validate_snapshot(staging)
            if current and not force and rows < current * min_ratio:
                raise IndexRefreshError(
                    f"Published snapshot has {rows} items, under {min_ratio:.0%} of the "
                    f"{current} already indexed; keeping the current index. Pass "
                    "force=True (--force) to accept it anyway."
                )
        except BaseException:
            _clean_leftovers(dest)
            raise
        _remove_sqlite_sidecars(staging)
        _remove_sqlite_sidecars(dest)
        os.replace(staging, dest)
        _write_snapshot_state(
            dest,
            {
                "url": src,
                "etag": etag,
                "last_modified": last_modified,
                "items": rows,
                "built_at": built_at,
            },
        )
    if state is None:
        reason = "fetched"
    elif existed and current is None:
        reason = "unreadable"
    else:
        reason = "changed"
    return RefreshResult(dest, True, etag, last_modified, rows, built_at, reason, cleaned)


#: Index ``meta`` key naming the sidecar snapshot last merged into it, so
#: ``umbra index fetch-thumbnails --if-changed`` can skip a no-op merge yet
#: still re-merge into a freshly swapped-in ``catalog.db`` (which lacks it).
THUMBS_SNAPSHOT_META_KEY = "thumbs_snapshot"


def _validate_thumbs_sidecar(path: Path) -> int:
    """Prove a downloaded ``catalog.thumbs.db`` is a readable, non-empty sidecar."""
    try:
        conn = sqlite3.connect(str(path))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
            rows = conn.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise IndexRefreshError(
            f"Downloaded thumbnail sidecar {path} is not readable: {exc}"
        ) from exc
    if check != "ok":
        raise IndexRefreshError(f"Downloaded thumbnail sidecar {path} failed quick_check: {check}")
    if rows <= 0:
        raise IndexRefreshError(f"Downloaded thumbnail sidecar {path} has no thumbnails.")
    return rows


def refresh_thumbnails_from_release(
    dest: str | os.PathLike | None = None,
    *,
    url: str | None = None,
    force: bool = False,
    session: Any = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> RefreshResult:
    """Replace the thumbnail sidecar with the published one only when it changed.

    The ``catalog.thumbs.db`` counterpart of :func:`refresh_from_release`: one
    ``HEAD`` compares the release asset's ``ETag`` / ``Last-Modified`` with the
    state recorded beside the sidecar (``catalog.thumbs.db.source.json``), and
    only a changed (or missing, or ``force``) sidecar is downloaded to
    ``<name>.next``, checked to be a readable non-empty sidecar, and swapped in
    atomically. ``dest`` defaults to :func:`default_thumbs_path`. ``items`` in
    the result is the sidecar's thumbnail count. Raises
    :class:`IndexRefreshError` (existing sidecar untouched) when the asset is
    unreachable or the download fails validation, and
    :class:`InsufficientSpaceError` when the directory cannot hold the new
    sidecar's ``Content-Length`` beside the current one (the old file stays
    until the new one is proven, so both coexist for a moment). Stale scratch
    files of earlier refreshes are removed first, as in
    :func:`refresh_from_release`. This only refreshes the file;
    :func:`merge_thumbnails_atomically` merges it into an index.
    """
    from .constants import CATALOG_INDEX_THUMBS_URL  # noqa: PLC0415
    from .download import download_url  # noqa: PLC0415

    sess = session or default_session()
    target = Path(dest) if dest is not None else default_thumbs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    src = url or CATALOG_INDEX_THUMBS_URL
    with _refresh_lock(target):
        cleaned = _clean_leftovers(target)
        etag, last_modified, length = _head_snapshot(sess, src)
        state = read_snapshot_state(target)
        if target.exists() and _same_snapshot(state, src, etag, last_modified) and not force:
            return RefreshResult(
                target, False, etag, last_modified, None, None, "unchanged", cleaned
            )

        _require_free_space(target.parent, length or 0, f"download {src.rsplit('/', 1)[-1]}")
        staging = target.with_name(target.name + ".next")
        try:
            download_url(src, staging, overwrite=True, session=sess, progress=progress)
            rows = _validate_thumbs_sidecar(staging)
        except BaseException as exc:
            _clean_leftovers(target)
            if isinstance(exc, IndexRefreshError) or not isinstance(exc, Exception):
                raise
            raise IndexRefreshError(
                f"Could not download the thumbnail sidecar {src!r}: {exc}"
            ) from exc
        _remove_sqlite_sidecars(staging)
        _remove_sqlite_sidecars(target)
        os.replace(staging, target)
        _write_snapshot_state(
            target, {"url": src, "etag": etag, "last_modified": last_modified, "items": rows}
        )
    reason = "fetched" if state is None else "changed"
    return RefreshResult(target, True, etag, last_modified, rows, None, reason, cleaned)


def _apply_thumbnails(conn: sqlite3.Connection, source: Path, *, overwrite: bool) -> int:
    """Run :meth:`CatalogIndex.import_thumbnails`' UPDATEs on ``conn``; no commit."""
    side = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        try:
            columns = {row[1] for row in side.execute("PRAGMA table_info(thumbnails)")}
            # A sidecar published before the provenance columns existed reads
            # as "unknown", which is what keeps it merging exactly as it did.
            recorded = ", ".join(
                col if col in columns else f"NULL AS {col}" for col in ("asset", "size")
            )
            rows = side.execute(f"SELECT href, png, {recorded} FROM thumbnails")
        except sqlite3.DatabaseError as exc:  # not a sidecar, or unreadable
            raise IndexSchemaError(
                f"{source} is not an umbra-py thumbnail sidecar "
                f"(no readable 'thumbnails' table): {exc}"
            ) from exc
        applied = 0
        for href, png, asset, size in rows:
            params: tuple[object, ...] = (sqlite3.Binary(png), asset, size, href)
            if not overwrite:
                params += (size, asset)
            cur = conn.execute(
                "UPDATE items SET thumbnail = ?, thumbnail_asset = ?, thumbnail_size = ? "
                f"WHERE href = ?{'' if overwrite else _KEEP_UNLESS_LARGER}",
                params,
            )
            applied += cur.rowcount
    finally:
        side.close()
    return applied


def _merge_growth(dest: Path, source: Path, *, overwrite: bool) -> tuple[int | None, int]:
    """``(rows, PNG bytes)`` a merge of ``source`` into ``dest`` will write.

    The sidecar rows that match an indexed acquisition *and* that the merge rule
    (:data:`_KEEP_UNLESS_LARGER`, or every match with ``overwrite``) would apply
    -- so re-merging a sidecar the index already holds costs ~0, not the whole
    file. ``length()`` of a BLOB reads only the record header, so this does no
    blob I/O. Falls back to ``(None, sidecar file size)`` if the query fails.
    """
    try:
        conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
        try:
            conn.execute("ATTACH DATABASE ? AS side", (f"file:{source}?mode=ro",))
            columns = {row[1] for row in conn.execute("PRAGMA side.table_info(thumbnails)")}
            size = "s.size" if "size" in columns else "NULL"
            asset = "s.asset" if "asset" in columns else "NULL"
            rule = (
                "1"
                if overwrite
                else f"(i.thumbnail IS NULL OR ({size} > i.thumbnail_size "
                f"AND i.thumbnail_asset IS {asset}))"
            )
            rows, size_bytes = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(s.png)), 0) FROM side.thumbnails s "
                f"JOIN items i ON i.href = s.href WHERE {rule}"
            ).fetchone()
            return rows, size_bytes
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None, source.stat().st_size


def merge_thumbnails_atomically(
    index_path: str | os.PathLike,
    sidecar: str | os.PathLike,
    *,
    overwrite: bool = False,
    snapshot: str | None = None,
) -> int:
    """Merge a thumbnail sidecar into an index so a failure leaves it as it was.

    :meth:`CatalogIndex.import_thumbnails` updates the index in place, which
    under write-ahead logging needs about *twice* the PNG payload free (every
    changed page goes to the ``-wal`` first, then the checkpoint grows the main
    file by the same amount). On the hosted volume that is ~2.4 GB for a
    1.2 GB sidecar, and a disk that fills mid-checkpoint leaves the serving
    index unable to open. This instead:

    1. removes a ``.merge`` copy an earlier failed run left behind;
    2. checks the directory has room for a copy of the index plus the PNG
       bytes the merge will write (:func:`_merge_growth`, +10%) -- raising
       :class:`InsufficientSpaceError`, index untouched, if not;
    3. copies the index to ``<name>.merge`` with SQLite's online backup (which
       includes pages still in the live ``-wal``) and applies the sidecar to the
       copy with ``journal_mode = OFF`` -- the copy is disposable, so it needs
       no rollback journal, which is what keeps the cost at one payload -- and
       ``temp_store = MEMORY`` so no SQLite temp file lands on the volume
       (or a small ``/tmp``); the merge is row-by-row UPDATEs and never sorts;
    4. records ``snapshot`` under :data:`THUMBS_SNAPSHOT_META_KEY`, runs
       ``PRAGMA quick_check`` and compares the row count with the original;
    5. swaps the copy in with ``os.replace`` (a rename: no space needed).

    When step 2 finds no row to apply (the index already holds everything
    the sidecar would give it) no copy is made; only ``snapshot`` is recorded.

    Any failure -- ``database or disk is full`` included -- deletes the copy and
    raises :class:`IndexRefreshError`, leaving the index byte-for-byte as it
    was. Like :func:`refresh_from_release`, the swap drops the old file's
    ``-wal`` / ``-shm``, so run it only while no server holds the index open
    (the container entrypoint does). Returns the number of thumbnails applied.
    """
    dest = Path(index_path)
    source = Path(sidecar)
    if not source.exists():
        raise FileNotFoundError(f"No thumbnail sidecar at {source}")
    with _refresh_lock(dest):
        _clean_leftovers(dest)
        rows_to_apply, growth = _merge_growth(dest, source, overwrite=overwrite)
        if rows_to_apply == 0:
            # Nothing to apply: a one-row meta write, which SQLite rolls back
            # cleanly if even that cannot fit, beats copying the whole index.
            try:
                with CatalogIndex(dest) as live:
                    if snapshot:
                        live.set_meta(THUMBS_SNAPSHOT_META_KEY, snapshot)
            except sqlite3.DatabaseError as exc:
                raise IndexRefreshError(
                    f"Recording the merged thumbnail snapshot in {dest} failed ({exc}); "
                    "the index was left as it was."
                ) from exc
            return 0
        need = _db_bytes(dest) + int(growth * (1 + _MERGE_HEADROOM))
        _require_free_space(dest.parent, need, f"merge {source.name} into a copy of {dest.name}")
        staging = dest.with_name(dest.name + ".merge")
        try:
            with CatalogIndex(dest) as live:
                expected = len(live)
                copy = sqlite3.connect(str(staging))
                try:
                    live._conn.backup(copy)
                    copy.execute("PRAGMA journal_mode = OFF")
                    copy.execute("PRAGMA temp_store = MEMORY")
                    applied = _apply_thumbnails(copy, source, overwrite=overwrite)
                    if snapshot:
                        copy.execute(
                            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                            (THUMBS_SNAPSHOT_META_KEY, snapshot),
                        )
                    copy.commit()
                    check = copy.execute("PRAGMA quick_check").fetchone()[0]
                    rows = copy.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                finally:
                    copy.close()
            if check != "ok":
                raise IndexRefreshError(f"Merged copy {staging} failed quick_check: {check}")
            if rows != expected:
                raise IndexRefreshError(
                    f"Merged copy {staging} has {rows} items; the index has {expected}."
                )
        except BaseException as exc:
            _clean_leftovers(dest)
            if isinstance(exc, IndexRefreshError) or not isinstance(exc, Exception):
                raise
            raise IndexRefreshError(
                f"Merging {source.name} into {dest.name} failed ({exc}); {dest} was left as it was."
            ) from exc
        _remove_sqlite_sidecars(dest)
        os.replace(staging, dest)
    return applied


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
        self._configure_connection()
        self._init_schema()

    def _configure_connection(self) -> None:
        """Tune the connection for concurrent, multi-process access.

        The index is no longer a private single-process cache: the *published*
        ``catalog.db`` snapshot (``umbra index fetch``) is read by ``umbra
        serve``, ``umbra demo`` and the MCP server while a CLI writer (``umbra
        index update`` / ``build`` / ``bake-*``) may be refreshing it in another
        process. Two PRAGMAs make that safe:

        - ``busy_timeout`` (always applied): a connection that finds the
          database locked waits up to :data:`_BUSY_TIMEOUT_MS` for the lock to
          clear instead of raising ``database is locked`` at once.
        - ``journal_mode=WAL`` (best-effort): under write-ahead logging, readers
          never block on a writer and a single writer never blocks readers, so
          the read-heavy shared-snapshot workload no longer contends with the
          occasional write. WAL is a persistent property of the file, so setting
          it once carries into every later open. It needs a writable database
          and directory -- which this class already requires, since it ensures
          the schema (a write) on every open -- so it never tightens the access
          the index already assumed. Should it be refused anyway (e.g. a
          read-only mount), the attempt is swallowed and the connection stays in
          its existing journal mode; ``busy_timeout`` still applies.
        """
        self._conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # A read-only medium can refuse the WAL switch; degrade to the
            # existing journal mode rather than failing to open the index.
            pass

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
        if "thumbnail" not in columns:  # v2 -> v3: baked SAR quicklook PNG
            self._conn.execute("ALTER TABLE items ADD COLUMN thumbnail BLOB")
        if "thumbnail_asset" not in columns:  # v3 -> v4: what that PNG is a picture of
            self._conn.execute("ALTER TABLE items ADD COLUMN thumbnail_asset TEXT")
            self._conn.execute("ALTER TABLE items ADD COLUMN thumbnail_size INTEGER")

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
            existed = item.href is not None and self._has(item.href)
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
        by_site: bool = False,
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
        ``limit`` caps how many *geocode calls* this run makes (to bake a large
        catalog in bounded batches); ``zoom`` is the Nominatim address
        granularity (3 = country ... 10 = city ... 18 = building). ``progress``,
        if given, is called with the running count of calls made.

        With ``by_site`` the bake geocodes **once per site** rather than once per
        acquisition: Umbra files every pass over a site under one task, so the
        passes sharing a task and a :data:`_SITE_CELL_DEGREES` cell are resolved
        together from their mean centroid and all take that one label (see
        :func:`_site_groups`). A repeat-imaged catalog is mostly repeat passes,
        so this collapses the throttled ~1 req/s call count by roughly the
        average passes-per-site -- which is what makes labelling a *whole*
        catalog (and shipping it pre-labelled in the published snapshot) practical
        rather than an overnight job. The label is a coarse place name for a
        footprint a few km across, so one per site is the same answer per-item
        geocoding would converge on; the default stays per-item.

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
            "SELECT href, task, min_lon, min_lat, max_lon, max_lat FROM items "
            "WHERE place IS NULL AND min_lon IS NOT NULL AND min_lat IS NOT NULL "
            "ORDER BY href"
        ).fetchall()
        groups = _site_groups(rows, by_site)
        if limit is not None:
            groups = groups[:limit]

        labelled = 0
        uncommitted = 0
        for processed, ((lat, lon), hrefs) in enumerate(groups, 1):
            label = geocoder(lat, lon)
            if label:
                self._conn.executemany(
                    "UPDATE items SET place = ? WHERE href = ?", [(label, h) for h in hrefs]
                )
                labelled += len(hrefs)
                uncommitted += len(hrefs)
                if uncommitted >= 50:
                    self._conn.commit()
                    uncommitted = 0
            if progress is not None:
                progress(processed)
        self._conn.commit()
        return labelled

    def bake_thumbnails(
        self,
        renderer: Callable[[UmbraItem], bytes | None] | None = None,
        *,
        asset: str = "GEC",
        max_size: int = PUBLISHED_THUMBNAIL_SIZE,
        limit: int | None = None,
        newest_first: bool = False,
        progress: Callable[[int], None] | None = None,
    ) -> int:
        """Render a small SAR quicklook per acquisition once and cache it.

        Every gallery, ``umbra demo`` preview and ``umbra serve`` quicklook
        otherwise re-streams a scene's cloud-optimized GeoTIFF overview from S3
        at *render* time, so the first view of a whole catalog is network-bound
        and slow. This bakes the preview ahead of time: for every indexed
        acquisition that carries the ``asset`` (default ``GEC``) but no thumbnail
        yet, it renders a ``max_size``-pixel PNG and stores the bytes in the
        additive ``thumbnail`` column, so a later
        :meth:`get_thumbnail` -- and the ``GET /artifacts/thumbnail/{id}.png``
        server endpoint that wraps it -- is an instant, offline file read.

        It is the render-side sibling of :meth:`bake_places`, and shares its
        discipline: **idempotent** (items whose ``thumbnail`` is still ``NULL``,
        *or* whose recorded ``thumbnail_size`` is smaller than this call's
        ``max_size``, are rendered -- so a weekly bump from 128 px to 512 px
        actually upgrades the sidecar instead of leaving the old bake in
        place), with ``limit`` capping how many are rendered this call (to bake
        a large catalog in bounded batches). An item whose asset can't be
        rendered (no ``asset``, a decode error, a network blip) is skipped --
        its thumbnail stays as it was so a later run retries it -- and one bad
        scene never aborts the batch, mirroring the gallery contact sheet.

        ``newest_first`` chooses *which* acquisitions a capped run spends its
        budget on: by default the batch is taken in ``href`` order, which is
        arbitrary with respect to time, so a bounded bake over a whole catalog
        leaves the freshest scenes -- the ones a demo or a monitoring view opens
        on -- unbaked the longest. With ``newest_first=True`` the most recently
        acquired are rendered first, which is what makes a per-run cap a
        *priority* rather than a lottery. Items with no acquisition date sort
        last, as they carry no claim to being recent.

        ``asset`` and ``max_size`` are recorded beside the bytes
        (``thumbnail_asset`` / ``thumbnail_size``, read back by
        :meth:`get_preview`), because a preview is only interchangeable with the
        picture a caller asked for when the two are of the same product: without
        the record every consumer had to *assume* the default bake. They describe
        what this call was asked to render, so an injected ``renderer`` that
        ignores them records a claim it did not honour.

        ``renderer`` is an injectable ``(UmbraItem) -> bytes | None`` callable
        returning PNG bytes (or ``None`` to skip); the default wraps
        :func:`umbra_py.viz._thumbnail_png`, which streams only the overview for
        ``max_size`` and needs the ``viz`` extra. Passing a stand-in keeps the
        whole path offline-testable. ``progress``, if given, is called with the
        running count of items processed. Returns the number newly thumbnailed.
        """
        if renderer is None:
            from .viz import _thumbnail_png  # noqa: PLC0415

            def renderer(item: UmbraItem) -> bytes | None:
                try:
                    return _thumbnail_png(item, asset=asset, max_size=max_size)
                except Exception:
                    # A single scene failing to render must not abort the bake;
                    # it stays NULL and is retried on the next run.
                    return None

        # A NULL acq_date must not outrank a real one under newest-first, so it
        # is ordered last explicitly (SQLite sorts NULLs first on DESC).
        order = "acq_date IS NULL, acq_date DESC, href" if newest_first else "href"
        rows = self._conn.execute(
            "SELECT href, doc, place FROM items "
            "WHERE href IN (SELECT href FROM item_assets WHERE asset = ?) "
            "AND (thumbnail IS NULL OR "
            "(thumbnail_asset = ? AND thumbnail_size IS NOT NULL "
            "AND thumbnail_size < ?)) "
            f"ORDER BY {order}",
            (asset.upper(), asset.upper(), max_size),
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]

        baked = 0
        for processed, (href, doc, place) in enumerate(rows, 1):
            item = UmbraItem.from_dict(json.loads(doc), href=href)
            item.place = place
            png = renderer(item)
            if png:
                self._conn.execute(
                    "UPDATE items SET thumbnail = ?, thumbnail_asset = ?, "
                    "thumbnail_size = ? WHERE href = ?",
                    (sqlite3.Binary(png), asset.upper(), max_size, href),
                )
                baked += 1
                if baked % 25 == 0:
                    self._conn.commit()
            if progress is not None:
                progress(processed)
        self._conn.commit()
        return baked

    def get_thumbnail(self, item_id: str) -> bytes | None:
        """Return the baked quicklook PNG for this STAC id, or ``None``.

        The retrieval complement to :meth:`bake_thumbnails`: an
        ``idx_items_id``-backed point lookup for the cached preview bytes (never
        loaded by :meth:`search`/:meth:`get`, which would bloat every
        :class:`~umbra_py.models.UmbraItem` with a PNG). ``None`` means the id is
        absent *or* its thumbnail has not been baked -- both mean "render it
        instead". If two sidecars share an id, the first by ``href`` order wins,
        as in :meth:`get`.
        """
        row = self._conn.execute(
            "SELECT thumbnail FROM items WHERE id = ? AND thumbnail IS NOT NULL "
            "ORDER BY href LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return bytes(row[0])

    def get_preview(self, item_id: str) -> BakedPreview | None:
        """Return the baked quicklook *and what it is a picture of*, or ``None``.

        The provenance-carrying form of :meth:`get_thumbnail`, for the one
        consumer that cannot treat a preview as interchangeable pixels:
        ``umbra describe --preview`` hands the picture to a vision model, so a
        reading of a ``CSI`` bake is not a reading of the ``GEC`` one that was
        asked for. It reads the same row, so the extra provenance costs nothing;
        a preview baked before the index recorded it reports ``None`` for both
        fields (see :class:`BakedPreview`) rather than claiming the default.
        """
        row = self._conn.execute(
            "SELECT thumbnail, thumbnail_asset, thumbnail_size FROM items "
            "WHERE id = ? AND thumbnail IS NOT NULL ORDER BY href LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return BakedPreview(png=bytes(row[0]), asset=row[1], max_size=row[2])

    def export_thumbnails(self, dest: str | os.PathLike) -> int:
        """Write every baked thumbnail to a transportable sidecar database.

        Baking a quicklook costs a cloud-optimized GeoTIFF overview streamed
        from S3 per acquisition, so it is the one derived artifact nobody should
        recompute: this writes the bytes already baked into ``dest``
        (:data:`_THUMBS_SCHEMA` -- ``href``, ``id``, ``png``) so they can be
        published beside ``catalog.db`` and merged into any other index with
        :meth:`import_thumbnails`. That is what makes the weekly publish
        *incremental*: each run re-imports the previous sidecar first and then
        bakes only the acquisitions added since, instead of re-streaming the
        whole archive every Monday.

        The sidecar is deliberately separate from the index (rather than a
        published ``catalog.db`` carrying its ``thumbnail`` column) because the
        pixels are far larger than the metadata and not every caller wants them.
        Writing is an upsert into an existing file, so exporting twice is safe.
        Each row carries the bake's ``asset`` and ``size`` beside the bytes, so
        the receiving index knows what it merged rather than assuming it (an
        older sidecar is widened in place before writing). Returns the number of
        thumbnails written.
        """
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        out = sqlite3.connect(str(target))
        try:
            out.executescript(_THUMBS_SCHEMA)
            _widen_thumbs_sidecar(out)
            rows = self._conn.execute(
                "SELECT href, id, thumbnail, thumbnail_asset, thumbnail_size FROM items "
                "WHERE thumbnail IS NOT NULL ORDER BY href"
            )
            written = 0
            for href, item_id, png, asset, size in rows:
                out.execute(
                    "INSERT INTO thumbnails (href, id, png, asset, size) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(href) DO UPDATE SET id = excluded.id, png = excluded.png, "
                    "asset = excluded.asset, size = excluded.size",
                    (href, item_id, sqlite3.Binary(png), asset, size),
                )
                written += 1
            out.commit()
        finally:
            out.close()
        return written

    def import_thumbnails(self, src: str | os.PathLike, *, overwrite: bool = False) -> int:
        """Merge a thumbnail sidecar (:meth:`export_thumbnails`) into this index.

        The consume side of the published ``catalog.thumbs.db``: it fills the
        ``thumbnail`` column for the acquisitions the sidecar covers, so
        ``umbra serve``'s ``GET /artifacts/thumbnail/{id}.png``, the ``umbra
        demo`` preview and a ``--local`` gallery all read local bytes without a
        single COG range read. Rows the index does not hold are ignored (a
        sidecar built from a newer crawl is not an error).

        A local bake is kept rather than clobbered -- with one exception the
        sidecar's own record makes safe: when both sides say what they are and
        the incoming preview is a *larger* bake of the *same* product, it wins.
        That is the case the published sidecar creates, since it is baked at
        :data:`PUBLISHED_THUMBNAIL_SIZE` (512 px): a merge
        used to keep whichever arrived first, which made the resolution of a
        preview a fact about the order two commands were run in. Where either
        side is unrecorded the two are not comparable and the local bake stays.
        ``overwrite=True`` replaces unconditionally, as before. Returns the
        number of thumbnails applied.

        The merge is one transaction: a failure part-way rolls back every row
        it applied, not only the ones SQLite itself rolls back on ``database or
        disk is full``. It runs in place,
        so it needs about twice the sidecar's PNG payload free under WAL; the
        boot refresh uses :func:`merge_thumbnails_atomically` instead.
        """
        source = Path(src)
        if not source.exists():
            raise FileNotFoundError(f"No thumbnail sidecar at {source}")
        try:
            applied = _apply_thumbnails(self._conn, source, overwrite=overwrite)
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()
        return applied

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
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
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

        The acquisition-property filters (``polarizations``, ``min_incidence`` /
        ``max_incidence``, ``max_resolution``) mean exactly what they do on
        :meth:`UmbraCatalog.search`. They are read from each item's stored STAC
        document (already reconstructed here) and applied in Python via
        :meth:`UmbraItem.matches_filters`, the same way the polygon test runs, so
        both backends agree without a schema change.
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
            if not item.matches_filters(
                polarizations=polarizations,
                min_incidence=min_incidence,
                max_incidence=max_incidence,
                max_resolution=max_resolution,
            ):
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
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
        limit: int | None = None,
        max_per_task: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Read-through search: the index for the bulk, a live delta for what's new.

        :meth:`search` is instant but only returns what the index already holds;
        :meth:`UmbraCatalog.search` is always current but re-walks the whole
        bucket every call. This is the transparent middle the codebase analysis
        named as "make the index the default path": the index answers the whole
        query from local SQL, and a *bounded*
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
        ``area``, ``fuzzy``, the acquisition-property filters ``polarizations`` /
        ``min_incidence`` / ``max_incidence`` / ``max_resolution``, ``limit``,
        ``max_per_task``) mean exactly what they
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
        delta_start: date | None
        if max_acq is not None:
            horizon = date.fromisoformat(max_acq) - timedelta(days=max(0, overlap_days))
            # Never walk older than the caller's own start bound.
            delta_start = max(horizon, start_d) if start_d is not None else horizon
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
            "polarizations": polarizations,
            "min_incidence": min_incidence,
            "max_incidence": max_incidence,
            "max_resolution": max_resolution,
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

    def _ranking_where(
        self,
        bbox: BBox | None,
        start: DateLike,
        end: DateLike,
        product_types: list[str] | None,
        area: str | None,
        fuzzy: bool,
    ) -> tuple[str, list[object]]:
        """Build the SQL ``WHERE`` clause :meth:`rank_sites` counts sites under.

        Mirrors the SQL-expressible half of :meth:`search`'s condition-building --
        the acquisition-date bound, the bounding-box overlap, the ``area`` (or
        ``fuzzy`` task-set) match and the product-asset membership -- so a
        ``GROUP BY task`` here counts the same rows a ``search`` with the same
        filters would yield. It always constrains to rows that can be *grouped and
        ranked*: a non-null ``task`` (the site key) and a non-null ``datetime``
        (a dated pass, exactly what :func:`umbra_py.showcase.select_featured_sites`
        counts), so the count is a site's depth rather than its row total. The
        polygon and acquisition-property filters are deliberately absent -- they
        run per item in Python, so :meth:`rank_sites` takes the uncapped-pool path
        when any is set rather than pushing an approximation into SQL.
        """
        conditions = ["task IS NOT NULL", "datetime IS NOT NULL"]
        params: list[object] = []

        start_d = _coerce_date(start)
        end_d = _coerce_date(end, is_end=True)
        if start_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date >= ?)")
            params.append(start_d.isoformat())
        if end_d is not None:
            conditions.append("(acq_date IS NULL OR acq_date <= ?)")
            params.append(end_d.isoformat())
        if area and fuzzy:
            # SQL LIKE can't express the token-wise fuzzy match; resolve the
            # matching task names in Python (same matcher as `search`) and
            # constrain to them. An empty match set means nothing can match.
            names = [
                row[0]
                for row in self._conn.execute(
                    "SELECT DISTINCT task FROM items WHERE task IS NOT NULL"
                )
            ]
            matched = matching_tasks(area, names)
            if not matched:
                conditions.append("1 = 0")
            else:
                placeholders = ", ".join("?" * len(matched))
                conditions.append(f"task IN ({placeholders})")
                params += matched
        elif area:
            conditions.append("LOWER(task) LIKE ? ESCAPE '\\'")
            params.append(f"%{_escape_like(area.lower())}%")
        if bbox:
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

        return " WHERE " + " AND ".join(conditions), params

    def rank_sites(
        self,
        *,
        bbox: BBox | None = None,
        intersects: Geometry | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
        top: int = 20,
        min_passes: int = 2,
        rank_by: str = "passes",
        active_since: DateLike = None,
        active_before: DateLike = None,
        first_since: DateLike = None,
        first_before: DateLike = None,
        max_revisit_days: float | None = None,
        median_revisit_days: float | None = None,
        min_span_days: float | None = None,
        max_span_days: float | None = None,
    ) -> list[SiteCoverage]:
        """Rank the most repeat-imaged sites across the *whole* index.

        The index-native form of :func:`umbra_py.coverage.rank_site_coverage`,
        and the reason it exists: that function ranks whatever pool it is handed,
        so ``umbra sites --local`` capped the pool at ``--limit`` acquisitions and
        a site with many passes just *outside* the first ``--limit`` rows read as
        shallower than it is. Umbra files every pass of a site under one task, so a
        site's depth is a ``GROUP BY task`` the index can answer over its entire
        contents -- no pool cap, so a deeply-imaged site is ranked by all its
        passes rather than by the arbitrary window a limit happened to admit.

        The ranking is :func:`umbra_py.showcase.select_featured_sites`' exactly --
        dated passes per task, most first, task name breaking ties, keeping those
        with at least ``min_passes`` -- so this, ``umbra sites`` and the featured
        gallery cannot disagree about what "most repeat-imaged" means. Each site
        is summarised by :func:`umbra_py.coverage.site_coverage`; the result is
        the ``top`` best, best-first.

        The filters mean exactly what they do on :meth:`search`. The
        SQL-expressible ones (``bbox``, ``start`` / ``end``, ``area`` / ``fuzzy``,
        ``product_types``) are counted directly in a ``GROUP BY``, so only the top
        tasks' documents are then read to summarise -- cheap even whole-archive.
        The polygon (``intersects``) and acquisition-property (``polarizations``,
        ``min_incidence`` / ``max_incidence`` / ``max_resolution``) filters run per
        item in Python, so when any is set this ranks the full *uncapped* matching
        stream instead: still whole-archive (no ``limit``), identical to the pool
        path, just without the cap this method exists to remove.

        ``rank_by`` is one of :data:`umbra_py.coverage.SITE_RANKINGS`. ``"passes"``
        (the default) is answerable in SQL -- a ``COUNT(*)`` per task orders the
        candidates, so only the top ``top`` tasks' documents are read.
        ``"comparable"`` ranks by *analysable* depth (``comparable_passes``), which
        depends on each pass's polarization inside the document JSON and so is not a
        ``COUNT``: every qualifying task's documents are read and summarised, then
        re-ranked by the analysable subset and truncated to ``top``. The *temporal*
        rankings ``"recency"`` (newest dated pass first), ``"span"`` (longest
        observation baseline first) and ``"cadence"`` (tightest typical revisit gap
        first) are likewise not the SQL ``COUNT`` order -- the top-by-count tasks are
        not the top-by-recency, -span or -cadence -- so they take the same
        read-every-task-then-re-rank path (the raw-count ``LIMIT`` is dropped when the
        ranking is not ``"passes"``, so a recently-active, long-baseline or
        tightly-revisited site outside the raw top-``top`` is not truncated before it
        can be promoted). That is heavier than the raw path (it reads every
        repeat-imaged task rather than the top ``top``), but still whole-archive and
        correct. All five rankings share :func:`umbra_py.coverage._rank_sort_key`, and
        the temporal trio reads the ``last`` / ``span_days`` / ``median_revisit_days``
        off the same summarised :class:`SiteCoverage` the pool path reduces from the
        passes, so this and the pool path order every ranking identically.

        ``min_passes`` gates on the same depth ``rank_by`` ranks by
        (:func:`umbra_py.coverage._min_passes_depth`): under ``"comparable"`` the
        ``HAVING COUNT(*) >= min_passes`` clause is a superset pre-filter (comparable
        depth is never above the raw count) and the true floor on ``comparable_passes``
        is applied in Python before the re-rank, so ``--rank-by comparable
        --min-passes N`` returns only sites whose differenceable series is at least
        ``N`` passes deep. Under ``"passes"`` the SQL floor is exact and this and the
        pool path qualify a site identically.

        ``active_since`` keeps only sites still imaged *on or after* that date -- a
        recency filter on each site's **newest** dated pass. It is answered in the
        same ``HAVING`` clause the pass-count floor is (``MAX(acq_date) >= ?``), so
        it costs nothing beyond the group already computed and is exact under either
        ranking (a site's latest pass is independent of the polarization grouping
        ``"comparable"`` re-ranks by). Whole-archive like the rest of this method,
        and byte-identical to the pool path's :func:`umbra_py.coverage.rank_site_coverage`
        recency gate. It is orthogonal to ``start`` / ``end`` (those bound which
        *rows* the ``GROUP BY`` counts; this selects whole sites by their latest and
        keeps every counted pass in the summary). ``None`` applies no recency filter.

        ``active_before`` is the complement -- keep only sites whose newest dated
        pass is *on or before* that date (a dormant series), answered by the twin
        ``MAX(acq_date) <= ?`` clause in the same ``HAVING``, so with ``active_since``
        the two bound the site's latest pass to a window. A span expression snaps to
        its last day (symmetric with ``end``). Byte-identical to the pool path's
        upper-recency gate. ``None`` applies no upper bound.

        ``first_since`` / ``first_before`` are the onset (first-seen) twins of the
        ``active_*`` pair -- they gate each site's **earliest** dated pass rather than
        its newest, selecting **newly-appeared** series (``first_since``, first pass on
        or after the date) and **long-established** ones (``first_before``, first pass
        on or before it); set together they bound the onset to a window. Like the
        recency pair they are pure SQL aggregates, answered by ``MIN(acq_date) >= ?`` /
        ``MIN(acq_date) <= ?`` clauses in the same ``HAVING`` -- costing nothing beyond
        the group already computed and exact under either ranking (a site's earliest
        pass, like its latest, does not depend on the polarization grouping
        ``"comparable"`` re-ranks by), so unlike the cadence and span filters they do
        *not* force the raw-count ``LIMIT`` to be dropped. ``MIN`` skips NULL
        ``acq_date``, so a group with no dated pass yields NULL and is dropped
        (``NULL >= ?`` / ``NULL <= ?`` is never true), matching ``select_featured_sites``
        dropping a site with no datable pass. ``first_before`` snaps a span expression
        to its last day (symmetric with ``active_before`` / ``end``). Byte-identical to
        the pool path's onset gate. ``None`` applies no onset filter.

        ``max_revisit_days`` keeps only sites revisited *at least this often* -- a
        cadence filter on each site's **worst-case** revisit gap. Unlike the recency
        and depth filters it is *not* a SQL aggregate: the worst gap is between
        *consecutive* passes (and, under ``"comparable"``, over the largest
        single-polarization subset the document JSON defines), which no ``HAVING``
        clause on a column expresses. It is applied in Python on the same per-task
        items this method already reads to summarise, using the same
        :func:`umbra_py.coverage._passes_cadence` the pool path uses -- so the two are
        byte-identical -- and when it is set the raw-count SQL ``LIMIT`` is dropped
        (as it is for the comparable ranking), because a site that passes the cadence
        filter but sits just outside the raw top-``top`` must not be truncated before
        the filter runs. Gated on the same depth ``rank_by`` measures (the analysable
        series' cadence under ``"comparable"``), orthogonal to the recency filters,
        and dropping a site with fewer than two passes in the gated series. ``None``
        applies no cadence filter; a non-positive value is a ``ValueError``.

        ``median_revisit_days`` is the *typical*-cadence twin of ``max_revisit_days`` --
        keep only sites whose **median** revisit gap is at most this many days (a site
        *usually* imaged often, tolerating the odd long outage the worst-case bound
        rejects). Like the worst-case filter it is *not* a SQL aggregate -- a median of
        consecutive gaps is no more a ``HAVING`` clause than a max of them -- so it is
        applied in Python on the same per-task items this method already reads, using the
        same :func:`umbra_py.coverage._passes_median_revisit` the pool path uses (so the
        two are byte-identical), gated on the analysable subset under ``"comparable"``,
        and it drops the raw-count SQL ``LIMIT`` when set (as the cadence and span
        filters do) so a usually-tight site outside the raw top-``top`` is promoted rather
        than truncated before the filter runs. A site with fewer than two passes in the
        gated series has no measurable cadence and is dropped. ``None`` applies no
        typical-cadence filter; a non-positive value is a ``ValueError``.

        ``min_span_days`` keeps only sites imaged over *at least this long* -- a
        baseline filter on each site's observation **span** (whole days from first
        dated pass to last). Like the cadence filter it is applied in Python rather
        than SQL: under ``"comparable"`` the span is over the largest
        single-polarization subset the document JSON defines (not a column
        expression), and keeping the two rankings byte-identical is worth more than a
        SQL ``HAVING`` on the raw case alone, so it uses the same
        :func:`umbra_py.coverage._passes_span` the pool path does on the same per-task
        items this method already reads, and drops the raw-count SQL ``LIMIT`` when
        set (as the comparable ranking and the cadence filter do) so a long-baseline
        site outside the raw top-``top`` is promoted rather than truncated before the
        filter runs. Gated on the same depth ``rank_by`` measures (the analysable
        series' span under ``"comparable"``), orthogonal to the recency and cadence
        filters, and dropping a site with fewer than two passes in the gated series.
        ``None`` applies no span filter; a non-positive value is a ``ValueError``.

        ``max_span_days`` is the upper twin of ``min_span_days`` -- keep only sites
        imaged over *at most this long* (a short-lived series), the complement of the
        floor, and set with it a window bounding each site's baseline
        (``min_span_days <= span <= max_span_days``), as ``active_since`` /
        ``active_before`` bound the newest pass. Like the floor it is applied in Python
        with the same :func:`umbra_py.coverage._passes_max_span` the pool path uses (so
        the two paths stay byte-identical), gated on the analysable subset under
        ``"comparable"``, drops the raw-count SQL ``LIMIT`` when set, and drops a site
        with no measurable span so the window admits only a confirmed baseline.
        ``None`` applies no span ceiling; a non-positive value is a ``ValueError``.
        """
        from .coverage import (  # noqa: PLC0415
            _check_max_revisit,
            _check_max_span,
            _check_median_revisit,
            _check_min_span,
            _check_ranking,
            _min_passes_depth,
            _passes_cadence,
            _passes_max_span,
            _passes_median_revisit,
            _passes_span,
            _rank_sort_key,
            rank_site_coverage,
            site_coverage,
        )

        _check_ranking(rank_by)
        _check_max_revisit(max_revisit_days)
        _check_median_revisit(median_revisit_days)
        _check_min_span(min_span_days)
        _check_max_span(max_span_days)
        if top <= 0:
            return []
        since = _coerce_date(active_since)
        # ``active_before`` snaps a span expression to its last day (``is_end``),
        # symmetric with ``end`` and with the pool path's upper-recency gate.
        before = _coerce_date(active_before, is_end=True)
        # The onset bounds gate the *earliest* pass (``MIN(acq_date)``); ``first_before``
        # snaps to a span's last day like ``active_before``, ``first_since`` to its first.
        first_since_date = _coerce_date(first_since)
        first_before_date = _coerce_date(first_before, is_end=True)

        if (
            intersects is not None
            or polarizations
            or any(v is not None for v in (min_incidence, max_incidence, max_resolution))
        ):
            pool = list(
                self.search(
                    bbox=bbox,
                    intersects=intersects,
                    start=start,
                    end=end,
                    product_types=product_types,
                    area=area,
                    fuzzy=fuzzy,
                    polarizations=polarizations,
                    min_incidence=min_incidence,
                    max_incidence=max_incidence,
                    max_resolution=max_resolution,
                )
            )
            return rank_site_coverage(
                pool,
                top=top,
                min_passes=min_passes,
                rank_by=rank_by,
                active_since=active_since,
                active_before=active_before,
                first_since=first_since,
                first_before=first_before,
                max_revisit_days=max_revisit_days,
                median_revisit_days=median_revisit_days,
                min_span_days=min_span_days,
                max_span_days=max_span_days,
            )

        where, params = self._ranking_where(bbox, start, end, product_types, area, fuzzy)
        # Raw-count ranking picks the candidates in SQL and caps at ``top``; the
        # analysable ranking cannot (comparable depth is not a COUNT), so it reads
        # every task with enough passes and re-ranks after summarising.
        # ``HAVING COUNT(*) >= min_passes`` gates raw pass count in SQL. Under the
        # comparable ranking that is a *superset* pre-filter -- comparable depth is
        # never above the raw count, so no qualifying task is dropped -- and the true
        # ``min_passes`` floor (on ``comparable_passes``) is applied in Python once
        # the documents are read, before the re-rank and truncation.
        # ``active_since`` gates the same group on its newest dated pass
        # (``MAX(acq_date) >= ?``), exact under either ranking -- a site's latest
        # pass does not depend on the comparable grouping -- and the SQL twin of the
        # pool path's recency gate. ``acq_date`` is a NULL-skipping ``MAX``, so a
        # group with no dated ``acq_date`` yields NULL and is dropped (``NULL >= ?``
        # is never true), which matches ``select_featured_sites`` dropping a site
        # with no datable newest pass.
        # ``active_before`` is the twin upper bound (``MAX(acq_date) <= ?``): a group
        # whose newest dated pass is after the cutoff is dropped, so the two clauses
        # together bound the site's latest pass to a window. A group with no dated
        # ``acq_date`` yields NULL, and ``NULL <= ?`` is never true, so it is dropped
        # either way -- matching ``select_featured_sites`` dropping an undatable site.
        # ``first_since`` / ``first_before`` gate the same group on its *earliest*
        # dated pass (``MIN(acq_date)``), the onset twins of the ``MAX(acq_date)``
        # recency clauses: a site whose first pass predates ``first_since`` (not
        # newly-appeared) or postdates ``first_before`` (not long-established) is
        # dropped. ``MIN`` skips NULL, so an undatable group yields NULL and is dropped
        # either way, matching ``select_featured_sites``. Pure aggregates like the
        # recency pair, so they need no full scan.
        having = "HAVING COUNT(*) >= ?"
        candidate_params: list[object] = [*params, min_passes]
        if since is not None:
            having += " AND MAX(acq_date) >= ?"
            candidate_params.append(since.isoformat())
        if before is not None:
            having += " AND MAX(acq_date) <= ?"
            candidate_params.append(before.isoformat())
        if first_since_date is not None:
            having += " AND MIN(acq_date) >= ?"
            candidate_params.append(first_since_date.isoformat())
        if first_before_date is not None:
            having += " AND MIN(acq_date) <= ?"
            candidate_params.append(first_before_date.isoformat())
        candidate_sql = (
            f"SELECT task FROM items{where} GROUP BY task {having} ORDER BY COUNT(*) DESC, task ASC"
        )
        # The raw ranking picks the top ``top`` candidates in SQL and caps there; any
        # other ranking cannot (its order is not the SQL ``COUNT`` order) -- the
        # comparable ranking (analysable depth is not a COUNT) and the temporal ones
        # (``recency`` orders by ``MAX(acq_date)``, ``span`` by the dated range, and
        # ``cadence`` by a median-consecutive-gap that is not a column at all, so the
        # top-by-count candidates are not the top-by-recency/span/cadence), nor can a
        # cadence filter (a worst-consecutive-gap is not a column) or a span filter
        # (kept in Python so the comparable-subset span matches the pool path exactly).
        # Any of them reads every qualifying task and re-ranks/truncates in Python.
        # ``rank_by != "passes"`` covers every non-default ranking, including the trio.
        needs_full_scan = (
            rank_by != "passes"
            or max_revisit_days is not None
            or median_revisit_days is not None
            or min_span_days is not None
            or max_span_days is not None
        )
        if not needs_full_scan:
            candidate_sql += " LIMIT ?"
            candidate_params.append(top)
        candidates = self._conn.execute(candidate_sql, candidate_params).fetchall()

        ranked: list[SiteCoverage] = []
        for (task,) in candidates:
            passes = []
            for href, doc, place in self._conn.execute(
                f"SELECT href, doc, place FROM items{where} AND task = ? ORDER BY datetime, href",
                [*params, task],
            ):
                item = UmbraItem.from_dict(json.loads(doc), href=href)
                item.place = place
                passes.append(item)
            # Cadence filter (worst-case revisit gap), applied on the same items the
            # summary reads and with the same ``_passes_cadence`` the pool path uses,
            # so the two paths are byte-identical. Gated on the analysable subset
            # under ``"comparable"``. A site with no measurable cadence is dropped.
            if max_revisit_days is not None and not _passes_cadence(
                passes, rank_by=rank_by, max_revisit_days=max_revisit_days
            ):
                continue
            # Typical-cadence filter (median revisit gap), the complement of the
            # worst-case one above, applied on the same items and with the same
            # ``_passes_median_revisit`` the pool path uses, so the two paths are
            # byte-identical. Gated on the analysable subset under ``"comparable"``.
            # A site with no measurable cadence is dropped.
            if median_revisit_days is not None and not _passes_median_revisit(
                passes, rank_by=rank_by, median_revisit_days=median_revisit_days
            ):
                continue
            # Span filter (observation baseline), applied on the same items the
            # summary reads and with the same ``_passes_span`` the pool path uses, so
            # the two paths are byte-identical. Gated on the analysable subset under
            # ``"comparable"``. A site with no measurable span is dropped.
            if min_span_days is not None and not _passes_span(
                passes, rank_by=rank_by, min_span_days=min_span_days
            ):
                continue
            # Span ceiling (upper baseline bound), the upper twin of the floor above,
            # applied on the same items with the same ``_passes_max_span`` the pool path
            # uses, so the two paths stay byte-identical. Gated on the analysable subset
            # under ``"comparable"``. A site with no measurable span is dropped, so a
            # ``min_span``/``max_span`` window admits only a confirmed baseline.
            if max_span_days is not None and not _passes_max_span(
                passes, rank_by=rank_by, max_span_days=max_span_days
            ):
                continue
            ranked.append(site_coverage(task, passes))
        if needs_full_scan:
            if rank_by != "passes":
                # Qualify on the same depth the ranking uses (``_min_passes_depth``):
                # a site whose raw count cleared the SQL floor but whose comparable
                # series is shallower than ``min_passes`` is dropped rather than
                # ranked last, so ``--rank-by comparable --min-passes N`` is
                # whole-archive *and* honest about analysable depth.
                ranked = [
                    c
                    for c in ranked
                    if _min_passes_depth(
                        comparable_passes=c.comparable_passes, passes=c.passes, rank_by=rank_by
                    )
                    >= min_passes
                ]
            # Re-rank and cap: the comparable ranking needs the sort (its order is not
            # the SQL COUNT order); the raw ranking with a cadence filter dropped the
            # SQL ``LIMIT``, so it needs the truncation. Sorting by ``_rank_sort_key``
            # under ``"passes"`` reproduces the SQL ``COUNT DESC, task ASC`` order, so
            # it is a no-op there beyond making the cap correct.
            ranked.sort(
                key=lambda c: _rank_sort_key(
                    comparable_passes=c.comparable_passes,
                    passes=c.passes,
                    task=c.task,
                    rank_by=rank_by,
                    # The temporal rankings order by these whole-site figures; the
                    # summary already computed them from the same passes the pool path
                    # reduces via ``_temporal_rank_figures``, so parsing them back here
                    # is byte-identical to that path (a ranking candidate always has a
                    # dated ``last``; ``span_days`` / ``median_revisit_days`` are
                    # ``None`` for a single-pass site).
                    last=date.fromisoformat(c.last) if c.last else None,
                    span_days=c.span_days,
                    median_revisit_days=c.median_revisit_days,
                )
            )
            ranked = ranked[:top]
        return ranked

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

    def get_by_href(self, href: str) -> UmbraItem | None:
        """Return the indexed item with this STAC sidecar URL, or ``None``.

        The href-keyed complement to :meth:`get`. MCP tools that take a
        ``stac_href`` from ``search_catalog`` look the snapshot up here so
        they do not re-fetch a live sidecar that may have grown extra
        assets or different product filenames than the index the search
        already answered from.
        """
        row = self._conn.execute(
            "SELECT href, doc, place FROM items WHERE href = ? LIMIT 1",
            (href,),
        ).fetchone()
        if row is None:
            return None
        stored_href, doc, place = row
        item = UmbraItem.from_dict(json.loads(doc), href=stored_href)
        item.place = place
        return item

    def distinct_ids(self) -> int:
        """Number of distinct STAC item ids (rows can repeat an id when one
        collect is published under both ``tasks/`` and ``task-data/``)."""
        return self._conn.execute("SELECT COUNT(DISTINCT id) FROM items").fetchone()[0]

    def stats(self) -> dict[str, object]:
        """Summary counts for ``umbra index info``: item count, acquisition-date
        span, number of distinct tasks, how many items carry a baked place label
        (``labeled``; see :meth:`bake_places`), how many carry a baked quicklook
        thumbnail (``thumbnailed``; see :meth:`bake_thumbnails`), and the date the
        index was last built (``built_at``, ``None`` for an index written before
        build stamping)."""
        items, start, end, tasks, labeled, thumbnailed = self._conn.execute(
            "SELECT COUNT(*), MIN(acq_date), MAX(acq_date), COUNT(DISTINCT task), "
            "COUNT(place), COUNT(thumbnail) FROM items"
        ).fetchone()
        return {
            "items": items,
            "start": start,
            "end": end,
            "tasks": tasks,
            "labeled": labeled,
            "thumbnailed": thumbnailed,
            "built_at": self.get_meta("built_at"),
        }
