"""Public-host watch and index opens: no shared-catalog writes, no filesystem paths.

Offline. ``watch_site`` / ``index_stats`` are plain functions, so this file does
not need the MCP SDK. A public host is ``configure(cog_streaming=False)``, the
same switch ``umbra serve --public`` sets.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from umbra_py import mcp_server as ms
from umbra_py import serve
from umbra_py.exceptions import IndexSchemaError
from umbra_py.index import CatalogIndex, _sqlite_ro_uri
from umbra_py.models import UmbraItem

DATA_DIR = Path(__file__).parent / "data"
ITEM_URL = "https://umbra-open-data-catalog.s3.amazonaws.com/x/item.stac.v2.json"


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())


@pytest.fixture
def public_mode():
    ms.configure(cog_streaming=False)
    yield
    ms.configure(cog_streaming=None)


def _items(sample: dict, n: int) -> list[UmbraItem]:
    items = []
    for i in range(n):
        doc = copy.deepcopy(sample)
        doc["id"] = f"{sample['id']}-{i}"
        href = ITEM_URL.replace("item.stac", f"item-{i}.stac")
        items.append(UmbraItem.from_dict(doc, href=href))
    return items


def _index(path: Path, items: list[UmbraItem]) -> None:
    with CatalogIndex(path) as idx:
        for item in items:
            idx.add(item)
        idx.commit()


def _watch_rows(path: Path) -> int:
    conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM meta WHERE key LIKE 'watch:%' ESCAPE '\\'"
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _fake_catalog(items: list[UmbraItem]):
    class _FakeCatalog:
        def search(self, **kwargs):
            return iter(list(items))

    return _FakeCatalog


# --------------------------------------------------------------------------
# CatalogIndex mode=ro
# --------------------------------------------------------------------------


def test_readonly_uri_is_mode_ro(tmp_path):
    path = tmp_path / "catalog.db"
    assert _sqlite_ro_uri(path) == f"file:{path.as_posix()}?mode=ro"


def test_readonly_open_reads_and_refuses_writes(tmp_path):
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        idx.set_meta("built_at", "2026-01-01")
        idx.commit()
    size = path.stat().st_size

    with CatalogIndex(path, readonly=True) as ro:
        assert ro.get_meta("built_at") == "2026-01-01"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.set_meta("watch:x", '{"seen": []}')
    assert path.stat().st_size == size

    # A maintenance open (index fetch / relocate) is still writable.
    with CatalogIndex(path) as idx:
        idx.set_meta("built_at", "2026-02-01")
        idx.commit()
    with CatalogIndex(path, readonly=True) as ro:
        assert ro.get_meta("built_at") == "2026-02-01"


def test_readonly_missing_file_does_not_create_one_or_name_it(tmp_path):
    path = tmp_path / "missing" / "catalog.db"
    with pytest.raises(FileNotFoundError) as exc:
        CatalogIndex(path, readonly=True)
    assert not path.exists()
    assert str(path) not in str(exc.value)
    assert "umbra index fetch" in str(exc.value)


def test_readonly_wrong_schema_fails_without_the_path(tmp_path):
    path = tmp_path / "catalog.db"
    sqlite3.connect(path).close()
    with pytest.raises(IndexSchemaError) as exc:
        CatalogIndex(path, readonly=True)
    assert str(path) not in str(exc.value)
    assert "read-only" in str(exc.value)


def test_default_open_still_writes(tmp_path):
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        idx.set_meta("watch:local", '{"seen": ["h"]}')
        idx.commit()
    assert _watch_rows(path) == 1


# --------------------------------------------------------------------------
# watch_site / index_stats
# --------------------------------------------------------------------------


def test_public_watch_does_not_persist_and_caps_items(
    sample_item_dict, public_mode, monkeypatch, tmp_path
):
    path = tmp_path / "catalog.db"
    items = _items(sample_item_dict, 60)
    _index(path, items)
    size = path.stat().st_size
    monkeypatch.setattr(ms, "default_index_path", lambda: path)

    out = ms.watch_site(local=True, name="site-a")
    assert out["first_run"] is True
    assert out["new_count"] == ms._PUBLIC_WATCH_ITEM_CAP
    assert len(out["new_items"]) == ms._PUBLIC_WATCH_ITEM_CAP
    assert out["new_total"] == 60
    assert "does not store" in out["note"]
    assert out["source"] == "local-index"
    assert _watch_rows(path) == 0
    assert path.stat().st_size == size

    # A second call is not a delta: nothing was stored.
    again = ms.watch_site(local=True, name="site-a")
    assert again["first_run"] is True
    assert again["new_total"] == 60
    assert _watch_rows(path) == 0


def test_public_watch_without_an_index_does_not_create_one(
    sample_item_dict, public_mode, monkeypatch, tmp_path
):
    path = tmp_path / "catalog.db"
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    monkeypatch.setattr(ms, "UmbraCatalog", _fake_catalog(_items(sample_item_dict, 1)))

    out = ms.watch_site(area="anywhere", local=False)
    assert out["new_total"] == 1
    assert out["new_count"] == 1
    assert out["source"] == "live-catalog"
    assert "note" in out
    assert not path.exists()


def test_public_watch_caps_the_name(sample_item_dict, public_mode, monkeypatch, tmp_path):
    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    huge = "n" * 500

    out = ms.watch_site(local=True, name=huge)
    assert out["watch"] == huge[: ms._PUBLIC_WATCH_NAME_MAX]
    assert len(out["watch"]) == ms._PUBLIC_WATCH_NAME_MAX
    assert huge not in json.dumps(out)
    assert "does not store" in out["note"]
    assert _watch_rows(path) == 0

    same = ms.watch_site(local=True, name=huge)
    assert same["watch"] == out["watch"]


def test_local_watch_still_persists(sample_item_dict, monkeypatch, tmp_path):
    ms.configure(cog_streaming=None)
    path = tmp_path / "catalog.db"
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    monkeypatch.setattr(ms, "UmbraCatalog", _fake_catalog(_items(sample_item_dict, 1)))

    first = ms.watch_site(area="anywhere", local=False)
    assert first["new_count"] == 1
    assert "note" not in first
    assert "new_total" not in first
    assert _watch_rows(path) == 1
    second = ms.watch_site(area="anywhere", local=False)
    assert second["new_count"] == 0
    assert second["first_run"] is False


def test_public_index_stats_redacts_the_path(sample_item_dict, public_mode, monkeypatch, tmp_path):
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(ms, "default_index_path", lambda: missing)
    absent = ms.index_stats()
    assert absent["available"] is False
    assert absent["path"] is None
    assert str(tmp_path) not in json.dumps(absent)

    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    present = ms.index_stats()
    assert present["available"] is True
    assert present["items"] == 1
    assert present["path"] is None
    assert str(path) not in json.dumps(present)


def test_local_index_stats_still_reports_the_path(sample_item_dict, monkeypatch, tmp_path):
    ms.configure(cog_streaming=None)
    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    out = ms.index_stats()
    assert out["path"] == str(path)
    assert out["items"] == 1


def test_public_errors_omit_filesystem_paths(public_mode, monkeypatch, tmp_path):
    path = tmp_path / "nested" / "catalog.db"
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    with pytest.raises(FileNotFoundError) as missing:
        ms.search_catalog(area="anywhere", local=True)
    assert str(path) not in str(missing.value)
    assert "umbra index fetch" in str(missing.value)

    with pytest.raises(ValueError) as named:
        ms.get_item("2025-05-17-17-43-46_UMBRA-08_GEC.tif")
    assert str(tmp_path) not in str(named.value)


def test_local_errors_still_name_the_index(monkeypatch, tmp_path):
    ms.configure(cog_streaming=None)
    path = tmp_path / "catalog.db"
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    with pytest.raises(FileNotFoundError) as missing:
        ms.search_catalog(area="anywhere", local=True)
    assert str(path) in str(missing.value)


def test_public_search_catalog_still_reads(sample_item_dict, public_mode, monkeypatch, tmp_path):
    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 3))
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    out = ms.search_catalog(local=True, limit=20)
    assert out["count"] == 3
    assert out["source"] == "local-index"
    assert _watch_rows(path) == 0


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp extra")
def test_public_index_stats_resource_redacts_the_path(
    sample_item_dict, public_mode, monkeypatch, tmp_path
):
    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 2))
    monkeypatch.setattr(ms, "default_index_path", lambda: path)
    server = ms.build_server()
    content = asyncio.run(server.read_resource("umbra://index/stats"))
    payload = json.loads(list(content)[0].content)
    assert payload["path"] is None
    assert payload["items"] == 2
    assert str(path) not in json.dumps(payload)


# --------------------------------------------------------------------------
# umbra serve
# --------------------------------------------------------------------------


def test_open_source_readonly_omits_the_path_and_refuses_writes(sample_item_dict, tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError) as exc:
        serve.open_source(missing, readonly=True)
    assert str(missing) not in str(exc.value)
    with pytest.raises(FileNotFoundError) as local_exc:
        serve.open_source(missing, readonly=False)
    assert str(missing) in str(local_exc.value)

    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    source = serve.open_source(path, readonly=True)
    try:
        assert len(list(source.search())) == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            source.set_meta("watch:serve", "{}")
    finally:
        source.close()


@pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("mcp") is None,
    reason="serve and mcp extras",
)
def test_public_serve_opens_the_index_readonly(sample_item_dict, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    seen: list[bool] = []
    real = serve.CatalogIndex

    def spy(db, readonly: bool = False):
        seen.append(readonly)
        return real(db, readonly=readonly)

    monkeypatch.setattr(serve, "CatalogIndex", spy)
    try:
        app = serve.build_app(path, public=True)
        with TestClient(app) as client:
            assert client.get("/search?limit=1").status_code == 200
            health = client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["ready"] is True
            assert str(path) not in health.text
        assert seen
        assert all(seen)
    finally:
        ms.configure(cog_streaming=None)


@pytest.mark.skipif(importlib.util.find_spec("fastapi") is None, reason="serve extra")
def test_local_serve_opens_the_index_writable(sample_item_dict, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    path = tmp_path / "catalog.db"
    _index(path, _items(sample_item_dict, 1))
    seen: list[bool] = []
    real = serve.CatalogIndex

    def spy(db, readonly: bool = False):
        seen.append(readonly)
        return real(db, readonly=readonly)

    monkeypatch.setattr(serve, "CatalogIndex", spy)
    app = serve.build_app(path)
    with TestClient(app) as client:
        assert client.get("/search?limit=1").status_code == 200
    assert seen
    assert all(flag is False for flag in seen)


@pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("mcp") is None,
    reason="serve and mcp extras",
)
def test_public_serve_missing_index_error_omits_the_path(tmp_path):
    from fastapi.testclient import TestClient

    missing = tmp_path / "no-such" / "catalog.db"
    try:
        app = serve.build_app(missing, public=True)
        with TestClient(app) as client:
            resp = client.get("/search?limit=1")
        assert resp.status_code == 503
        assert str(tmp_path) not in resp.text
        assert "No local index" in resp.text
    finally:
        ms.configure(cog_streaming=None)


@pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("mcp") is None,
    reason="serve and mcp extras",
)
def test_public_health_schema_error_omits_the_path(tmp_path):
    from fastapi.testclient import TestClient

    bad = tmp_path / "catalog.db"
    sqlite3.connect(bad).close()
    try:
        app = serve.build_app(bad, public=True)
        with TestClient(app) as client:
            health = client.get("/healthz")
        assert health.status_code == 503
        assert str(bad) not in health.text
    finally:
        ms.configure(cog_streaming=None)
