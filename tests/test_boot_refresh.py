"""Warm boot serves the catalog it already has; the refresh swaps in later.

Railway stops the previous deployment before the new one can mount the volume,
so the process has to answer before the weekly snapshot download finishes.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from umbra_py.exceptions import IndexRefreshError  # noqa: E402
from umbra_py.index import (  # noqa: E402
    CatalogIndex,
    IndexLease,
    RefreshResult,
    attach_index_lease,
    detach_index_lease,
    publish_sqlite,
    refresh_serving_catalog,
)
from umbra_py.models import UmbraItem  # noqa: E402
from umbra_py.serve import build_app  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"


def _item(item_id: str) -> UmbraItem:
    sample = json.loads((DATA_DIR / "sample_item.json").read_text())
    doc = copy.deepcopy(sample)
    doc["id"] = item_id
    return UmbraItem.from_dict(
        doc,
        href=f"https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/tasks/Testville/{item_id}.stac.v2.json",
    )


def _catalog(path: Path, item_id: str) -> Path:
    with CatalogIndex(path) as idx:
        idx.add(_item(item_id))
    return path


def _ids(path: Path) -> list[str]:
    with CatalogIndex(path) as idx:
        return [item.id for item in idx.search()]


def test_publish_waits_for_the_open_reader_then_swaps(tmp_path):
    live = _catalog(tmp_path / "catalog.db", "old-id")
    staging = _catalog(tmp_path / "catalog.db.next", "new-id")
    lease = IndexLease()
    attach_index_lease(live, lease)
    reader = CatalogIndex(live)
    done = threading.Event()
    about_to_open = threading.Event()
    second_saw: list[list[str]] = []

    def _swap() -> None:
        with lease.writing():
            publish_sqlite(staging, live)
        done.set()

    def _second() -> None:
        about_to_open.set()
        with CatalogIndex(live) as idx:
            second_saw.append([item.id for item in idx.search()])

    worker = threading.Thread(target=_swap)
    second = threading.Thread(target=_second)
    worker.start()
    try:
        time.sleep(0.05)
        assert not done.is_set()
        assert [item.id for item in reader.search()] == ["old-id"]
        # A request that arrives while the publish is waiting must not slip
        # in ahead of it and keep the reader count from reaching zero.
        second.start()
        assert about_to_open.wait(1)
        time.sleep(0.05)
        assert second_saw == []
        reader.close()
        assert done.wait(2)
        second.join(2)
    finally:
        if not done.is_set():
            reader.close()
        worker.join(2)
        if second.ident is not None:
            second.join(2)
        detach_index_lease(live)
    assert second_saw == [["new-id"]]
    assert _ids(live) == ["new-id"]
    assert not staging.exists()


def test_refresh_failure_leaves_the_current_catalog(tmp_path, monkeypatch):
    live = _catalog(tmp_path / "catalog.db", "old-id")

    def _boom(*_args, **_kwargs):
        raise IndexRefreshError("release unreachable")

    monkeypatch.setattr("umbra_py.index.refresh_from_release", _boom)
    monkeypatch.setattr("umbra_py.index.refresh_thumbnails_from_release", _boom)
    outcome = refresh_serving_catalog(live)
    assert outcome.startswith("kept:")
    assert _ids(live) == ["old-id"]
    assert not (tmp_path / "catalog.db.next").exists()
    assert not (tmp_path / "catalog.db.merge").exists()


def test_refresh_swaps_a_validated_snapshot_in_one_rename(tmp_path, monkeypatch):
    live = _catalog(tmp_path / "catalog.db", "old-id")

    def _fetch(path, **_kwargs):
        staging = Path(path).with_name(Path(path).name + ".next")
        _catalog(staging, "new-id")
        return RefreshResult(staging, True, "etag-new", None, 1, "2026-10-26", "changed", {})

    def _no_thumbs(*_args, **_kwargs):
        raise IndexRefreshError("no sidecar on the release")

    monkeypatch.setattr("umbra_py.index.refresh_from_release", _fetch)
    monkeypatch.setattr("umbra_py.index.refresh_thumbnails_from_release", _no_thumbs)
    outcome = refresh_serving_catalog(live)
    assert outcome.startswith("swapped")
    assert _ids(live) == ["new-id"]
    assert not (tmp_path / "catalog.db.next").exists()


def test_warm_server_answers_before_the_refresh_finishes(tmp_path, monkeypatch):
    path = _catalog(tmp_path / "catalog.db", "old-id")
    entered = threading.Event()
    release = threading.Event()

    def _block(_path, **_kwargs):
        entered.set()
        assert release.wait(5)
        return "unchanged"

    monkeypatch.setattr("umbra_py.index.refresh_serving_catalog", _block)
    app = build_app(path, refresh=True)
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            health = client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["ready"] is True
            assert client.get("/livez").status_code == 200
            found = client.get("/search", params={"limit": 10}).json()
            assert [feature["id"] for feature in found["features"]] == ["old-id"]
            assert not release.is_set()
            release.set()
    finally:
        release.set()
        detach_index_lease(path)


def test_cold_start_readiness_fails_until_the_catalog_is_published(tmp_path, monkeypatch):
    path = tmp_path / "catalog.db"
    entered = threading.Event()
    release = threading.Event()

    def _fetch_then_publish(target, **_kwargs):
        entered.set()
        assert release.wait(5)
        _catalog(Path(target), "cold-id")
        return "swapped"

    monkeypatch.setattr("umbra_py.index.refresh_serving_catalog", _fetch_then_publish)
    app = build_app(path, refresh=True)
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            assert client.get("/livez").status_code == 200
            waiting = client.get("/healthz")
            assert waiting.status_code == 503
            assert waiting.json()["ready"] is False
            assert waiting.json()["status"] == "starting"
            release.set()
            deadline = time.monotonic() + 2
            ready = waiting
            while time.monotonic() < deadline:
                ready = client.get("/healthz")
                if ready.status_code == 200:
                    break
                time.sleep(0.02)
            assert ready.status_code == 200
            assert ready.json()["ready"] is True
            assert client.get("/livez").status_code == 200
    finally:
        release.set()
        detach_index_lease(path)


def test_refresh_failure_keeps_the_warm_server_on_the_current_catalog(tmp_path, monkeypatch):
    path = _catalog(tmp_path / "catalog.db", "old-id")
    entered = threading.Event()

    def _fail(_path, **_kwargs):
        entered.set()
        raise RuntimeError("snapshot download failed")

    monkeypatch.setattr("umbra_py.index.refresh_serving_catalog", _fail)
    app = build_app(path, refresh=True)
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            time.sleep(0.05)
            health = client.get("/healthz")
            assert health.status_code == 200
            assert health.json()["items"] >= 1
            found = client.get("/search", params={"limit": 10}).json()
            assert [feature["id"] for feature in found["features"]] == ["old-id"]
    finally:
        detach_index_lease(path)
