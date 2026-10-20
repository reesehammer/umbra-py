"""Preview blobs must not sit in the item row ahead of the columns search reads."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from umbra_py.index import (
    _INLINE_RELOCATE_MAX_BYTES,
    _SCHEMA_VERSION,
    _THUMBS_OFF_ROW,
    _THUMBS_RELOCATE_DEFERRED,
    CatalogIndex,
)
from umbra_py.models import UmbraItem

DATA_DIR = Path(__file__).parent / "data"

_V7 = """
CREATE TABLE items (
    href TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    task TEXT,
    datetime TEXT,
    acq_date TEXT,
    min_lon REAL,
    min_lat REAL,
    max_lon REAL,
    max_lat REAL,
    doc TEXT NOT NULL,
    place TEXT,
    thumbnail BLOB,
    thumbnail_asset TEXT,
    thumbnail_size INTEGER,
    datetime_suspect INTEGER NOT NULL DEFAULT 0,
    created TEXT,
    published TEXT,
    canonical INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE item_assets (
    href TEXT NOT NULL,
    asset TEXT NOT NULL,
    PRIMARY KEY (href, asset)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _sample() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())


def _v7(path: Path, *, blobs: list[tuple[str, bytes]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_V7)
    doc = _sample()
    for item_id, png in blobs:
        body = dict(doc)
        body["id"] = item_id
        conn.execute(
            "INSERT INTO items (href, id, task, datetime, acq_date, doc, place, thumbnail, "
            "thumbnail_asset, thumbnail_size, created, published, canonical) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                f"https://example.test/{item_id}.json",
                item_id,
                "Testville",
                "2024-01-02T00:00:00Z",
                "2024-01-02",
                json.dumps(body),
                "Testville",
                png,
                "GEC",
                32,
                "2024-01-01T00:00:00Z",
                "2024-01-03T00:00:00Z",
            ),
        )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return " ".join(str(row[-1]) for row in rows)


_SEARCH = (
    "SELECT href, doc, place, datetime_suspect, created, published FROM items "
    "ORDER BY (task IS NULL), task, acq_date, datetime, id, href LIMIT 1"
)


def test_schema7_thumbnails_move_off_the_row_without_a_rebuild(tmp_path):
    path = tmp_path / "catalog.db"
    png = b"\x89PNG" + b"x" * 20_000
    # More rows than one relocate batch. A move that copied every preview
    # before freeing the old pages would grow the file by the whole payload.
    n = 24
    _v7(path, blobs=[(f"scene-{i}", png) for i in range(n)])
    before = os.path.getsize(path)
    payload = n * len(png)
    raw = sqlite3.connect(path)
    cold = _plan(raw, _SEARCH.replace(" LIMIT 1", ""))
    raw.close()
    assert "TEMP B-TREE" in cold

    with CatalogIndex(path) as idx:
        assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION
        assert idx.get_meta(_THUMBS_OFF_ROW) == "1"
        left = idx._conn.execute(
            "SELECT COUNT(*) FROM items WHERE length(thumbnail) IS NOT NULL"
        ).fetchone()
        assert left[0] == 0
        moved = idx._conn.execute(
            "SELECT thumbnail FROM item_thumbnails WHERE id = ?", ("scene-0",)
        ).fetchone()
        assert bytes(moved[0]) == png
        assert idx.get_thumbnail("scene-0") == png
        assert "TEMP B-TREE" not in _plan(idx._conn, _SEARCH)
        assert "idx_items_search" in _plan(idx._conn, _SEARCH)
    # Batches commit and checkpoint so the next insert reuses the freelist.
    # Growth is about one batch plus the search index. A second copy of the
    # payload (or a VACUUM, which needs a full extra file) would add `payload`.
    after = os.path.getsize(path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            after += sidecar.stat().st_size
    assert after - before < payload


def test_search_limit_and_stats_do_not_read_the_thumbnail_column(tmp_path):
    path = tmp_path / "catalog.db"
    _v7(path, blobs=[("scene-a", b"png-a"), ("scene-b", b"png-b")])
    read: list[tuple[str, str]] = []

    def auth(action, arg1, arg2, _db, _source):
        if action == sqlite3.SQLITE_READ and arg2 and "thumbnail" in str(arg2):
            read.append((str(arg1), str(arg2)))
        return sqlite3.SQLITE_OK

    with CatalogIndex(path) as idx:
        moved = idx._conn.execute(
            "SELECT id, thumbnail FROM item_thumbnails ORDER BY id"
        ).fetchall()
        assert [(row[0], bytes(row[1])) for row in moved] == [
            ("scene-a", b"png-a"),
            ("scene-b", b"png-b"),
        ]
        idx._conn.set_authorizer(auth)
        traced: list[str] = []
        idx._conn.set_trace_callback(traced.append)
        page = list(idx.search(limit=1))
        idx.stats()
        idx.temporal_extent()
        assert len(page) == 1
        assert page[0].id in {"scene-a", "scene-b"}
        selects = [sql for sql in traced if "LIMIT" in sql.upper() and "FROM items" in sql]
        assert selects, traced
        assert read == []


def test_large_schema7_defers_then_relocates_on_request(tmp_path, monkeypatch):
    monkeypatch.setattr("umbra_py.index._INLINE_RELOCATE_MAX_BYTES", 0)
    assert _INLINE_RELOCATE_MAX_BYTES  # the real ceiling stays; the patch is the gate
    path = tmp_path / "catalog.db"
    png = b"y" * 2048
    _v7(path, blobs=[("scene-a", png)])
    with CatalogIndex(path) as idx:
        assert idx.get_meta(_THUMBS_RELOCATE_DEFERRED) == "1"
        still = idx._conn.execute("SELECT thumbnail FROM items").fetchone()[0]
        assert bytes(still) == png
        # Hot stats must not name the inline blob while the move is owed.
        seen: list[str] = []
        idx._conn.set_trace_callback(seen.append)
        stats = idx.stats()
        assert stats["thumbnailed"] == 1
        assert not any("COUNT(thumbnail)" in sql for sql in seen)
        moved = idx.relocate_thumbnails()
        assert moved == 1
        assert idx.get_meta(_THUMBS_OFF_ROW) == "1"
        assert idx._conn.execute("SELECT thumbnail FROM items").fetchone()[0] is None
        assert idx.get_thumbnail("scene-a") == png
        assert idx.stats()["thumbnailed"] == 1


def test_search_round_trip_keeps_published_and_created(tmp_path):
    doc = _sample()
    doc["id"] = "scene-a"
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        item = UmbraItem.from_dict(doc, href="https://example.test/scene-a.json")
        item.published = item.created
        idx.add(item)
        idx.commit()
        found = next(iter(idx.search(limit=1)))
        assert found.id == "scene-a"
    # Re-open: the limit still comes back from SQL, not a Python truncate of a full sort.
    with CatalogIndex(path) as idx:
        sqls: list[str] = []
        idx._conn.set_trace_callback(sqls.append)
        list(idx.search(limit=1))
        assert any("LIMIT" in sql for sql in sqls)


def test_polygon_filter_does_not_sql_limit_before_the_python_test(tmp_path):
    """A SQL LIMIT in front of a polygon reject would return a short page."""
    from umbra_py._geometry import parse_geometry

    doc = _sample()
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        for i in range(3):
            body = dict(doc)
            body["id"] = f"scene-{i}"
            idx.add(UmbraItem.from_dict(body, href=f"https://example.test/{i}.json"))
        idx.commit()
        sqls: list[str] = []
        idx._conn.set_trace_callback(sqls.append)
        geom = parse_geometry(
            {
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
            }
        )
        list(idx.search(intersects=geom, limit=1))
        item_selects = [sql for sql in sqls if "FROM items" in sql and "datetime_suspect" in sql]
        assert item_selects
        assert all("LIMIT" not in sql.upper() for sql in item_selects)
