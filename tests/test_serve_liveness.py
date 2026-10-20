"""Liveness stays up when a catalog read does not, and the production app answers GETs."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from umbra_py import serve  # noqa: E402
from umbra_py.index import CatalogIndex  # noqa: E402
from umbra_py.models import UmbraItem  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"


@pytest.fixture
def index_path(tmp_path) -> Path:
    sample = json.loads((DATA_DIR / "sample_item.json").read_text())
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        for i in range(3):
            doc = copy.deepcopy(sample)
            doc["id"] = f"item-{i}"
            href = (
                "https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/tasks/"
                f"Testville/2024-01-0{i + 1}-00-00-00_UMBRA-0{i + 1}/x{i}.stac.v2.json"
            )
            idx.add(UmbraItem.from_dict(doc, href=href))
    return path


def test_public_app_get_health_and_collections_return_quickly(index_path):
    pytest.importorskip("mcp")
    app = serve.build_app(index_path, public=True)
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            health = await client.get("/healthz")
            collections = await client.get("/collections")
            live = await client.get("/livez")
            return health, collections, live, time.perf_counter() - started

    health, collections, live, elapsed = asyncio.run(_run())
    assert health.status_code == 200
    assert health.json()["ready"] is True
    assert collections.status_code == 200
    assert collections.json()["collections"]
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert elapsed < 2.0


def test_healthz_is_cached_for_an_unchanged_index(index_path, monkeypatch):
    calls = {"n": 0}
    original = CatalogIndex.stats

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(CatalogIndex, "stats", counting)
    client = TestClient(serve.build_app(index_path))
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert calls["n"] == 1


def test_livez_stays_up_when_readiness_hits_its_deadline(index_path, monkeypatch):
    monkeypatch.setenv("UMBRA_REQUEST_TIMEOUT_S", "0.2")

    def hang(*_args, **_kwargs):
        time.sleep(2)
        raise FileNotFoundError("still reading")

    monkeypatch.setattr(serve, "open_source", hang)
    # A context-managed client keeps the portal open. Without it, closing the
    # portal joins the still-running catalog thread and the clock includes
    # that join, not the response the client already had.
    with TestClient(serve.build_app(index_path)) as client:
        started = time.perf_counter()
        ready = client.get("/healthz")
        live = client.get("/livez")
        elapsed = time.perf_counter() - started
    assert ready.status_code == 503
    assert "Timeout" in ready.json()["error"]
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert elapsed < 1.5


def test_a_saturated_catalog_returns_503_without_waiting(index_path, monkeypatch):
    monkeypatch.setenv("UMBRA_DB_CONCURRENCY", "1")
    started = threading.Event()
    release = threading.Event()

    def block(*_args, **_kwargs):
        started.set()
        release.wait(5)
        raise FileNotFoundError("done")

    monkeypatch.setattr(serve, "open_source", block)
    app = serve.build_app(index_path)
    transport = httpx.ASGITransport(app=app)

    async def _run():
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.get("/healthz"))
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.02)
            assert started.is_set()
            second = await client.get("/search", params={"limit": 1})
            release.set()
            first_response = await first
            return first_response, second

    held, busy = asyncio.run(_run())
    assert busy.status_code == 503
    assert "busy" in busy.json()["detail"].lower()
    assert held.status_code == 200
