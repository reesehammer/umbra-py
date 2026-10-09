"""Refreshing the served index from the published snapshot (PRD P1-1).

The hosted API keeps catalog.db on a persistent volume; the entrypoint used to
fetch only when no file existed, so the API stayed on its first snapshot.
``refresh_from_release`` replaces it whenever the release asset changes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import namedtuple

import pytest
import responses

from umbra_py import index as index_mod
from umbra_py.exceptions import IndexRefreshError, InsufficientSpaceError
from umbra_py.index import (
    THUMBS_SNAPSHOT_META_KEY,
    CatalogIndex,
    _normalize_etag,
    _refresh_lock,
    merge_thumbnails_atomically,
    read_snapshot_state,
    refresh_from_release,
    refresh_thumbnails_from_release,
    snapshot_state_path,
)
from umbra_py.models import UmbraItem

URL = "https://example.test/releases/download/catalog-index/catalog.db"


def _db_bytes(tmp_path, name: str, n: int, built_at: str = "2026-10-05") -> bytes:
    path = tmp_path / name
    with CatalogIndex(path) as idx:
        for i in range(n):
            idx.add(
                UmbraItem.from_dict(
                    {"id": f"id-{i}", "bbox": [0, 0, 1, 1], "properties": {}, "assets": {}},
                    href=f"https://h/{name}/{i}.stac.v2.json",
                )
            )
        idx.set_meta("built_at", built_at)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    return path.read_bytes()


def _serve(body: bytes, etag: str) -> None:
    headers = {"ETag": etag, "Last-Modified": "Mon, 05 Oct 2026 15:09:48 GMT"}
    responses.add(
        responses.HEAD, URL, status=200, headers={**headers, "Content-Length": str(len(body))}
    )
    responses.add(
        responses.GET,
        URL,
        body=body,
        status=200,
        headers={**headers, "Content-Length": str(len(body))},
    )


def _count(path) -> int:
    with CatalogIndex(path) as idx:
        return len(idx)


@responses.activate
def test_first_fetch_records_the_snapshot(tmp_path):
    dest = tmp_path / "data" / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"v1"')
    result = refresh_from_release(dest, url=URL)
    assert result.changed and result.reason == "fetched" and result.items == 3
    assert _count(dest) == 3
    state = read_snapshot_state(dest)
    assert state["etag"] == "v1" and state["built_at"] == "2026-10-05"
    assert snapshot_state_path(dest).name == "catalog.db.source.json"


@responses.activate
def test_unchanged_snapshot_is_one_head_request(tmp_path):
    dest = tmp_path / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"v1"')
    refresh_from_release(dest, url=URL)
    calls = len(responses.calls)
    result = refresh_from_release(dest, url=URL)
    assert not result.changed and result.reason == "unchanged"
    assert len(responses.calls) == calls + 1
    assert responses.calls[-1].request.method == "HEAD"


@responses.activate
def test_stale_volume_index_is_replaced(tmp_path):
    """The production failure: an old catalog.db with no recorded snapshot."""
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_db_bytes(tmp_path, "old.db", 2))
    _serve(_db_bytes(tmp_path, "new.db", 5), '"v2"')
    result = refresh_from_release(dest, url=URL)
    assert result.changed and result.reason == "fetched"
    assert _count(dest) == 5


@responses.activate
def test_shrinking_snapshot_is_refused_unless_forced(tmp_path):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_db_bytes(tmp_path, "big.db", 10))
    small = _db_bytes(tmp_path, "small.db", 2)
    _serve(small, '"v3"')
    with pytest.raises(IndexRefreshError, match="keeping the current index"):
        refresh_from_release(dest, url=URL)
    assert _count(dest) == 10
    assert not (tmp_path / "catalog.db.next").exists()
    _serve(small, '"v3"')
    assert refresh_from_release(dest, url=URL, force=True).items == 2


@responses.activate
def test_corrupt_download_leaves_the_index_untouched(tmp_path):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_db_bytes(tmp_path, "good.db", 4))
    _serve(b"not a database" * 100, '"bad"')
    with pytest.raises(IndexRefreshError):
        refresh_from_release(dest, url=URL)
    assert _count(dest) == 4
    assert read_snapshot_state(dest) is None


@responses.activate
def test_unreachable_release_raises_refresh_error(tmp_path):
    responses.add(responses.HEAD, URL, status=503)
    with pytest.raises(IndexRefreshError, match="Could not reach"):
        refresh_from_release(tmp_path / "catalog.db", url=URL)


# -- ETag normalization -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, clean",
    [
        ('"0x8DE1"', "0x8DE1"),
        ('W/"0x8DE1"', "0x8DE1"),
        (" 0x8DE1 ", "0x8DE1"),
        ("0x8DE1", "0x8DE1"),
        ('""', None),
        (None, None),
    ],
)
def test_normalize_etag(raw, clean):
    assert _normalize_etag(raw) == clean


@responses.activate
def test_state_is_recorded_without_quotes(tmp_path):
    dest = tmp_path / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), 'W/"0xABC"')
    result = refresh_from_release(dest, url=URL)
    assert result.etag == "0xABC"
    assert json.loads(snapshot_state_path(dest).read_text())["etag"] == "0xABC"


@responses.activate
def test_quoted_state_from_an_older_volume_still_matches(tmp_path):
    """State written before normalization kept the quotes; the same release
    asset must still read as unchanged (no spurious re-download)."""
    dest = tmp_path / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"0xABC"')
    refresh_from_release(dest, url=URL)
    state_path = snapshot_state_path(dest)
    state = json.loads(state_path.read_text())
    state_path.write_text(json.dumps({**state, "etag": '"0xABC"'}))
    assert read_snapshot_state(dest)["etag"] == "0xABC"
    calls = len(responses.calls)
    assert refresh_from_release(dest, url=URL).reason == "unchanged"
    assert len(responses.calls) == calls + 1


@responses.activate
def test_quoted_state_still_refreshes_on_a_new_etag(tmp_path):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_db_bytes(tmp_path, "old.db", 3))
    snapshot_state_path(dest).write_text(
        json.dumps(
            {
                "url": URL,
                "etag": '"0xOLD"',
                "last_modified": "Mon, 05 Oct 2026 15:09:48 GMT",
            }
        )
    )
    _serve(_db_bytes(tmp_path, "new.db", 4), '"0xNEW"')
    result = refresh_from_release(dest, url=URL)
    assert result.changed and result.reason == "changed" and _count(dest) == 4


# -- the thumbnail sidecar ----------------------------------------------------

THUMBS_URL = "https://example.test/releases/download/catalog-index/catalog.thumbs.db"


def _item(i: int) -> UmbraItem:
    gec = {"GEC": {"href": f"https://h/{i}_GEC.tif", "type": "image/tiff"}}
    return UmbraItem.from_dict(
        {"id": f"id-{i}", "bbox": [0, 0, 1, 1], "properties": {}, "assets": gec},
        href=f"https://h/{i}.stac.v2.json",
    )


def _index(path, n: int = 2) -> None:
    with CatalogIndex(path) as idx:
        for i in range(n):
            idx.add(_item(i))


def _thumbs_bytes(tmp_path, name: str, png: bytes, n: int = 2) -> bytes:
    src = tmp_path / f"{name}.src.db"
    _index(src, n)
    out = tmp_path / f"{name}.thumbs.db"
    with CatalogIndex(src) as idx:
        idx.bake_thumbnails(lambda item: png)
        idx.export_thumbnails(out)
    return out.read_bytes()


def _serve_thumbs(body: bytes, etag: str) -> None:
    headers = {"ETag": etag, "Last-Modified": "Mon, 05 Oct 2026 16:00:00 GMT"}
    responses.add(
        responses.HEAD,
        THUMBS_URL,
        status=200,
        headers={**headers, "Content-Length": str(len(body))},
    )
    responses.add(
        responses.GET,
        THUMBS_URL,
        body=body,
        status=200,
        headers={**headers, "Content-Length": str(len(body))},
    )


def _gets(url: str) -> int:
    return sum(1 for c in responses.calls if c.request.method == "GET" and c.request.url == url)


@responses.activate
def test_thumbs_first_fetch_records_the_snapshot(tmp_path):
    dest = tmp_path / "catalog.thumbs.db"
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"one"), '"t1"')
    result = refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    assert result.changed and result.reason == "fetched" and result.items == 2
    assert read_snapshot_state(dest)["etag"] == "t1"
    assert snapshot_state_path(dest).name == "catalog.thumbs.db.source.json"


@responses.activate
def test_thumbs_unchanged_is_one_head_request(tmp_path):
    dest = tmp_path / "catalog.thumbs.db"
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"one"), '"t1"')
    refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    calls = len(responses.calls)
    result = refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    assert not result.changed and result.reason == "unchanged"
    assert len(responses.calls) == calls + 1
    assert responses.calls[-1].request.method == "HEAD"


@responses.activate
def test_stale_volume_thumbs_sidecar_is_replaced_when_the_etag_changes(tmp_path):
    """The production failure: an old sidecar on the volume, never refreshed."""
    dest = tmp_path / "catalog.thumbs.db"
    dest.write_bytes(_thumbs_bytes(tmp_path, "old", b"old", n=1))
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"one"), '"t1"')
    assert refresh_thumbnails_from_release(dest, url=THUMBS_URL).reason == "fetched"
    responses.reset()
    _serve_thumbs(_thumbs_bytes(tmp_path, "v2", b"two", n=3), '"t2"')
    result = refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    assert result.changed and result.reason == "changed" and result.items == 3
    assert read_snapshot_state(dest)["etag"] == "t2"


@responses.activate
def test_thumbs_absent_or_corrupt_asset_keeps_the_old_sidecar(tmp_path):
    dest = tmp_path / "catalog.thumbs.db"
    old = _thumbs_bytes(tmp_path, "old", b"old")
    dest.write_bytes(old)
    responses.add(responses.HEAD, THUMBS_URL, status=404)
    with pytest.raises(IndexRefreshError, match="Could not reach"):
        refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    responses.reset()
    _serve_thumbs(b"not a database" * 100, '"bad"')
    with pytest.raises(IndexRefreshError):
        refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    assert dest.read_bytes() == old
    assert not (tmp_path / "catalog.thumbs.db.next").exists()
    assert read_snapshot_state(dest) is None


def _fetch_thumbs(db) -> object:
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    return CliRunner().invoke(
        cli_mod.cli,
        ["index", "fetch-thumbnails", "--db", str(db), "--url", THUMBS_URL, "--if-changed"],
    )


def _thumb(db) -> bytes | None:
    with CatalogIndex(db) as idx:
        return idx.get_thumbnail("id-0")


@responses.activate
def test_cli_if_changed_merges_new_sidecar_and_skips_when_nothing_moved(tmp_path):
    db = tmp_path / "catalog.db"
    _index(db)
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"one"), '"t1"')
    first = _fetch_thumbs(db)
    assert first.exit_code == 0, first.output
    assert "Merged 2 thumbnail(s)" in first.output and _thumb(db) == b"one"

    second = _fetch_thumbs(db)
    assert second.exit_code == 0, second.output
    assert "Thumbnails are current (snapshot t1)" in second.output
    assert _gets(THUMBS_URL) == 1


@responses.activate
def test_cli_if_changed_remerges_into_a_swapped_in_index(tmp_path):
    """The published catalog.db carries no thumbnails: after the index refresh
    swaps it in, the unchanged sidecar must be merged again."""
    db = tmp_path / "catalog.db"
    _index(db)
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"one"), '"t1"')
    assert _fetch_thumbs(db).exit_code == 0
    db.unlink()
    _index(db)
    assert _thumb(db) is None
    result = _fetch_thumbs(db)
    assert result.exit_code == 0, result.output
    assert "Merged 2 thumbnail(s)" in result.output and _thumb(db) == b"one"
    assert _gets(THUMBS_URL) == 1


@responses.activate
def test_cli_if_changed_keeps_and_merges_the_existing_sidecar_when_the_check_fails(tmp_path):
    db = tmp_path / "catalog.db"
    _index(db)
    (tmp_path / "catalog.thumbs.db").write_bytes(_thumbs_bytes(tmp_path, "old", b"old"))
    responses.add(responses.HEAD, THUMBS_URL, status=503)
    result = _fetch_thumbs(db)
    assert result.exit_code == 0, result.output
    assert "Thumbnail refresh failed" in result.output and _thumb(db) == b"old"


@responses.activate
def test_cli_if_changed_without_any_sidecar_fails_cleanly(tmp_path):
    db = tmp_path / "catalog.db"
    _index(db)
    responses.add(responses.HEAD, THUMBS_URL, status=404)
    result = _fetch_thumbs(db)
    assert result.exit_code != 0 and "Could not reach" in result.output
    assert not (tmp_path / "catalog.thumbs.db").exists()


# -- disk safety: stale leftovers, free space, a disk that fills mid-merge ------

_Usage = namedtuple("_Usage", "total used free")


def _free_space(monkeypatch, free: int) -> None:
    monkeypatch.setattr(index_mod.shutil, "disk_usage", lambda _path: _Usage(free, 0, free))


def _thumbs_count(db) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(thumbnail) FROM items").fetchone()[0]
    finally:
        conn.close()


@responses.activate
def test_stale_refresh_leftovers_are_removed_before_downloading(tmp_path):
    """An interrupted boot leaves a staged download, its resume sidecars and a
    half-written merge copy; none is ever resumed, all of it fills the volume."""
    dest = tmp_path / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"v1"')
    refresh_from_release(dest, url=URL)
    stale = {
        "catalog.db.next": 4096,
        "catalog.db.next.part": 1 << 20,
        "catalog.db.next.part.etag": 6,
        "catalog.db.next-journal": 512,
        "catalog.db.merge": 2 << 20,
        "catalog.db.merge-journal": 512,
        "catalog.db.source.json.tmp": 10,
    }
    for name, size in stale.items():
        (tmp_path / name).write_bytes(b"x" * size)
    unrelated = tmp_path / "catalog.db.bak"
    unrelated.write_bytes(b"keep me")

    result = refresh_from_release(dest, url=URL)
    assert result.reason == "unchanged"
    assert result.cleaned == stale
    assert not any((tmp_path / name).exists() for name in stale)
    assert unrelated.read_bytes() == b"keep me"
    assert _count(dest) == 3


@responses.activate
def test_cleanup_checkpoints_the_live_wal_instead_of_deleting_it(tmp_path):
    """The live index's -wal may hold committed rows; a reader may hold it open."""
    dest = tmp_path / "catalog.db"
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"v1"')
    refresh_from_release(dest, url=URL)
    reader = CatalogIndex(dest)  # a running server's connection
    try:
        writer = CatalogIndex(dest)
        writer.add(_item(99))
        writer.commit()  # committed, still only in the -wal
        assert (tmp_path / "catalog.db-wal").stat().st_size > 0
        refresh_from_release(dest, url=URL)
        writer.close()
        assert len(reader) == 4
    finally:
        reader.close()
    assert _count(dest) == 4


@responses.activate
def test_cleanup_never_touches_a_refresh_in_progress(tmp_path):
    dest = tmp_path / "catalog.db"
    part = tmp_path / "catalog.db.next.part"
    part.write_bytes(b"still downloading")
    _serve(_db_bytes(tmp_path, "v1.db", 3), '"v1"')
    with _refresh_lock(dest):
        with pytest.raises(IndexRefreshError, match="in progress"):
            refresh_from_release(dest, url=URL)
    assert part.read_bytes() == b"still downloading"
    assert not dest.exists()


@responses.activate
def test_unreadable_index_is_refetched_even_when_the_snapshot_is_unchanged(tmp_path):
    """The incident: a full disk damaged catalog.db while its ETag stayed current."""
    dest = tmp_path / "catalog.db"
    body = _db_bytes(tmp_path, "v1.db", 3)
    _serve(body, '"v1"')
    refresh_from_release(dest, url=URL)
    dest.write_bytes(b"\0" * 8192)
    _serve(body, '"v1"')
    result = refresh_from_release(dest, url=URL)
    assert result.changed and result.reason == "unreadable"
    assert _count(dest) == 3


@responses.activate
def test_index_download_is_skipped_without_room_for_its_content_length(tmp_path, monkeypatch):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_db_bytes(tmp_path, "old.db", 4))
    body = _db_bytes(tmp_path, "new.db", 5)
    _serve(body, '"v2"')
    _free_space(monkeypatch, index_mod._FREE_SPACE_RESERVE + len(body) - 1)
    with pytest.raises(InsufficientSpaceError, match="download catalog.db"):
        refresh_from_release(dest, url=URL)
    assert _gets(URL) == 0
    assert _count(dest) == 4
    assert not (tmp_path / "catalog.db.next.part").exists()

    _free_space(monkeypatch, index_mod._FREE_SPACE_RESERVE + len(body))
    assert refresh_from_release(dest, url=URL).items == 5


@responses.activate
def test_thumbs_download_is_skipped_without_room_and_the_old_sidecar_kept(tmp_path, monkeypatch):
    dest = tmp_path / "catalog.thumbs.db"
    old = _thumbs_bytes(tmp_path, "old", b"old")
    dest.write_bytes(old)
    _serve_thumbs(_thumbs_bytes(tmp_path, "v2", b"two", n=3), '"t2"')
    _free_space(monkeypatch, 0)
    with pytest.raises(InsufficientSpaceError):
        refresh_thumbnails_from_release(dest, url=THUMBS_URL)
    assert _gets(THUMBS_URL) == 0
    assert dest.read_bytes() == old


def test_merge_is_skipped_without_room_for_a_copy_plus_the_payload(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    _index(db)
    png = b"one" * 1000
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", png))
    payload = 2 * len(png)
    assert index_mod._merge_growth(db, sidecar, overwrite=False) == (2, payload)
    need = index_mod._db_bytes(db) + int(payload * (1 + index_mod._MERGE_HEADROOM))
    _free_space(monkeypatch, need + index_mod._FREE_SPACE_RESERVE - 1)
    before = db.read_bytes()
    with pytest.raises(InsufficientSpaceError, match="merge catalog.thumbs.db"):
        merge_thumbnails_atomically(db, sidecar)
    assert db.read_bytes() == before
    assert not (tmp_path / "catalog.db.merge").exists()

    _free_space(monkeypatch, need + index_mod._FREE_SPACE_RESERVE)
    assert merge_thumbnails_atomically(db, sidecar) == 2


def test_merge_estimate_counts_only_the_rows_the_merge_will_write(tmp_path):
    """A sidecar the index already holds costs a copy, not a second payload --
    otherwise a 5 GB volume would refuse every sidecar-only re-merge."""
    db = tmp_path / "catalog.db"
    _index(db, n=3)
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"x" * 5000, n=3))
    assert index_mod._merge_growth(db, sidecar, overwrite=False) == (3, 15000)
    merge_thumbnails_atomically(db, sidecar, snapshot="t1")
    assert index_mod._merge_growth(db, sidecar, overwrite=False) == (0, 0)
    assert index_mod._merge_growth(db, sidecar, overwrite=True) == (3, 15000)


def test_a_merge_with_nothing_to_apply_records_the_snapshot_without_a_copy(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    _index(db, n=3)
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"x" * 5000, n=3))
    merge_thumbnails_atomically(db, sidecar, snapshot="t1")
    _free_space(monkeypatch, 0)  # a copy could not fit; none is needed
    assert merge_thumbnails_atomically(db, sidecar, snapshot="t2") == 0
    with CatalogIndex(db) as idx:
        assert idx.get_meta(THUMBS_SNAPSHOT_META_KEY) == "t2"
    assert _thumbs_count(db) == 3


def _fill_disk_during_merge(monkeypatch, spare_pages: int = 0) -> None:
    """Make the merge's UPDATEs hit SQLITE_FULL -- the production error,
    ``database or disk is full`` -- once ``spare_pages`` more pages are used."""
    real = index_mod._apply_thumbnails

    def capped(conn, source, *, overwrite):
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count = {pages + spare_pages}")
        return real(conn, source, overwrite=overwrite)

    monkeypatch.setattr(index_mod, "_apply_thumbnails", capped)


def test_disk_full_during_merge_leaves_the_index_readable_and_unchanged(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    _index(db, n=4)
    with CatalogIndex(db) as idx:
        idx.bake_thumbnails(lambda item: b"old" if item.id == "id-0" else None)
        idx.set_meta(THUMBS_SNAPSHOT_META_KEY, "t0")
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"\x89PNG" * 20_000, n=4))
    _fill_disk_during_merge(monkeypatch, spare_pages=25)

    with pytest.raises(IndexRefreshError, match="database or disk is full"):
        merge_thumbnails_atomically(db, sidecar, overwrite=True, snapshot="t1")

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert _count(db) == 4
    assert _thumb(db) == b"old" and _thumbs_count(db) == 1
    with CatalogIndex(db) as idx:
        assert idx.get_meta(THUMBS_SNAPSHOT_META_KEY) == "t0"
    assert not (tmp_path / "catalog.db.merge").exists()


def test_atomic_merge_applies_the_sidecar_and_records_its_snapshot(tmp_path):
    db = tmp_path / "catalog.db"
    _index(db, n=3)
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"one", n=3))
    assert merge_thumbnails_atomically(db, sidecar, snapshot="t1") == 3
    assert _thumb(db) == b"one" and _thumbs_count(db) == 3
    with CatalogIndex(db) as idx:
        assert idx.get_meta(THUMBS_SNAPSHOT_META_KEY) == "t1"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("catalog.db")) == [
        "catalog.db",
        "catalog.db.lock",
    ]


def test_in_place_import_rolls_back_a_merge_that_fails_part_way(tmp_path):
    """SQLite rolls back by itself on ``SQLITE_FULL``, but not on an error raised
    between statements -- here a hand-built sidecar whose last row has no PNG.
    ``import_thumbnails`` used to leave that transaction open, so the
    ``with CatalogIndex(...)`` exit committed the rows already applied."""
    db = tmp_path / "catalog.db"
    _index(db, n=3)
    sidecar = tmp_path / "foreign.thumbs.db"
    conn = sqlite3.connect(sidecar)
    conn.execute("CREATE TABLE thumbnails (href TEXT, id TEXT, png BLOB)")
    conn.executemany(
        "INSERT INTO thumbnails VALUES (?, ?, ?)",
        [(f"https://h/{i}.stac.v2.json", f"id-{i}", b"one" if i < 2 else None) for i in range(3)],
    )
    conn.commit()
    conn.close()
    with pytest.raises(TypeError):
        with CatalogIndex(db) as idx:
            idx.import_thumbnails(sidecar)
    assert _thumbs_count(db) == 0


@responses.activate
def test_cli_disk_full_merge_fails_loudly_and_keeps_serving_the_index(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    _index(db)
    _serve_thumbs(_thumbs_bytes(tmp_path, "v1", b"\x89PNG" * 20_000), '"t1"')
    _fill_disk_during_merge(monkeypatch)
    result = _fetch_thumbs(db)
    assert result.exit_code != 0
    assert "database or disk is full" in result.output and "left as it was" in result.output
    assert _count(db) == 2 and _thumb(db) is None
    assert (tmp_path / "catalog.thumbs.db").exists()

    monkeypatch.undo()
    retry = _fetch_thumbs(db)
    assert retry.exit_code == 0, retry.output
    assert _thumb(db) == b"\x89PNG" * 20_000


@responses.activate
def test_cli_merge_without_room_is_skipped_with_a_clear_line(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    _index(db)
    (tmp_path / "catalog.thumbs.db").write_bytes(_thumbs_bytes(tmp_path, "old", b"old"))
    responses.add(responses.HEAD, THUMBS_URL, status=503)
    _free_space(monkeypatch, 0)
    result = _fetch_thumbs(db)
    assert result.exit_code != 0
    assert "Not enough free space" in result.output
    assert "to merge catalog.thumbs.db" in result.output
    assert "Skipped; the current files were kept." in result.output
    assert _count(db) == 2 and _thumb(db) is None


def test_free_space_is_measured_on_the_index_directory(tmp_path, monkeypatch):
    seen = []
    real = shutil.disk_usage

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(index_mod.shutil, "disk_usage", spy)
    db = tmp_path / "data" / "catalog.db"
    db.parent.mkdir()
    _index(db)
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"one"))
    merge_thumbnails_atomically(db, sidecar)
    assert seen == [db.parent]


def test_a_meta_only_merge_on_a_full_disk_raises_a_refresh_error(tmp_path, monkeypatch):
    """Found replaying the incident: an index whose WAL already held the merged
    rows took the no-copy path, and its one meta write escaped as a raw
    ``sqlite3.OperationalError`` traceback instead of a clean refresh error."""
    db = tmp_path / "catalog.db"
    _index(db, n=2)
    sidecar = tmp_path / "catalog.thumbs.db"
    sidecar.write_bytes(_thumbs_bytes(tmp_path, "v1", b"one"))
    merge_thumbnails_atomically(db, sidecar, snapshot="t1")

    def full(self, key, value):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(CatalogIndex, "set_meta", full)
    with pytest.raises(IndexRefreshError, match="left as it was"):
        merge_thumbnails_atomically(db, sidecar, snapshot="t2")
    monkeypatch.undo()
    with CatalogIndex(db) as idx:
        assert idx.get_meta(THUMBS_SNAPSHOT_META_KEY) == "t1"
