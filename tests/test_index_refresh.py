"""Refreshing the served index from the published snapshot (PRD P1-1).

The hosted API keeps catalog.db on a persistent volume; the entrypoint used to
fetch only when no file existed, so the API stayed on its first snapshot.
``refresh_from_release`` replaces it whenever the release asset changes.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import responses

from umbra_py.exceptions import IndexRefreshError
from umbra_py.index import (
    CatalogIndex,
    _normalize_etag,
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
    responses.add(responses.HEAD, URL, status=200, headers=headers)
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
    responses.add(responses.HEAD, THUMBS_URL, status=200, headers=headers)
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
