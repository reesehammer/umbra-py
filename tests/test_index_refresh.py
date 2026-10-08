"""Refreshing the served index from the published snapshot (PRD P1-1).

The hosted API keeps catalog.db on a persistent volume; the entrypoint used to
fetch only when no file existed, so the API stayed on its first snapshot.
``refresh_from_release`` replaces it whenever the release asset changes.
"""

from __future__ import annotations

import sqlite3

import pytest
import responses

from umbra_py.exceptions import IndexRefreshError
from umbra_py.index import (
    CatalogIndex,
    read_snapshot_state,
    refresh_from_release,
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
    assert state["etag"] == '"v1"' and state["built_at"] == "2026-10-05"
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
