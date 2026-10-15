"""Republished-collect linking: group, pick a canonical copy, filter, report."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from umbra_py.cli import cli
from umbra_py.duplicates import (
    annotate_republished,
    choose_canonical,
    duplicate_groups,
)
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem
from umbra_py.serve import build_app, item_to_stac, parse_query
from umbra_py.validate import sanitize_index

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}
TASK = "987c2f8a-75d6-4a72-9271-be89f7aacbc7"
COLLECT = "38614c21-d2ce-4664-92c3-84f717d1fffd"


def _href(kind: str, item_id: str) -> str:
    stem = f"2024-06-01-00-00-00_UMBRA-05/{item_id}.stac.v2.json"
    if kind == "task-data":
        return f"https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/task-data/{TASK}/{stem}"
    if kind == "tasks":
        return (
            "https://umbra-open-data-catalog.s3.amazonaws.com/sar-data/tasks/"
            f"Nepal-Disaster-Coverage_September-2026/{TASK}/{stem}"
        )
    return f"https://umbra-open-data-catalog.s3.amazonaws.com/open-data/{item_id}/{stem}"


def _assets(*products: str, item_id: str) -> dict:
    out: dict = {}
    for product in products:
        ext = {"GEC": "tif", "CSI": "tif", "SIDD": "nitf", "SICD": "nitf", "CPHD": "cphd"}[product]
        name = f"{item_id}_{product}.{ext}"
        media = "image/tiff; application=geotiff" if ext == "tif" else "application/octet-stream"
        out[name] = {"href": f"https://example.com/{name}", "type": media}
    return out


def _copy(
    item_id: str,
    *,
    kind: str = "task-data",
    dt: str | None = "2024-06-01T00:00:00Z",
    created: str | None = None,
    version: str | None = None,
    software: str | None = None,
    products: tuple[str, ...] = ("GEC",),
    task: str = TASK,
    collect: str = COLLECT,
) -> UmbraItem:
    props: dict = {"umbra:task_id": task, "umbra:collect_id": collect}
    if dt is not None:
        props["datetime"] = dt
    if created is not None:
        props["created"] = created
    if version is not None:
        props["processing:version"] = version
    if software is not None:
        props["processing:software"] = {"Umbra SAR Processor": software}
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "properties": props,
        "bbox": [0, 0, 1, 1],
        "geometry": SQUARE,
        "assets": _assets(*products, item_id=item_id),
    }
    return UmbraItem.from_dict(doc, href=_href(kind, item_id))


def _index(tmp_path, *items: UmbraItem) -> CatalogIndex:
    idx = CatalogIndex(tmp_path / "catalog.db")
    for item in items:
        idx.add(item)
    idx.commit()
    return idx


def test_newer_created_beats_more_products_and_task_data_prefix():
    """The Nepal pair: the later STAC record wins, even without CPHD."""
    task_data = _copy(
        "d3021736-ec74-4af9-8920-310db60ad194",
        kind="task-data",
        created="2026-09-04T00:00:00Z",
        version="5.2.6",
        products=("GEC", "SICD", "CPHD"),
    )
    campaign = _copy(
        "882d68cc-b95b-48e7-b215-5b08804c25ee",
        kind="tasks",
        created="2026-09-22T00:00:00Z",
        version="5.2.8",
        products=("GEC", "SICD"),
    )
    assert choose_canonical([task_data, campaign]).id == campaign.id


def test_newer_created_wins_the_thacker_pass_pair():
    task_data = _copy(
        "76be7474-2393-44f4-93ba-8d691b83a6ac",
        kind="task-data",
        dt="2025-06-01T00:00:00Z",
        created="2026-03-04T00:00:00Z",
        version="4.35.1",
        products=("GEC", "CSI", "SIDD", "SICD", "CPHD"),
    )
    campaign = _copy(
        "65693df0-0770-4306-ac98-44562109ee99",
        kind="tasks",
        dt="2025-06-01T00:00:00Z",
        created="2025-07-15T00:00:00Z",
        version="4.21.0",
        products=("GEC", "SIDD", "SICD"),
    )
    assert choose_canonical([campaign, task_data]).id == task_data.id


def test_rank_falls_through_version_datetime_products_prefix_and_id():
    same = dict(created="2026-01-01T00:00:00Z", dt="2024-06-01T00:00:00Z")
    older_ver = _copy("older-ver", kind="tasks", version="5.2.8", **same)
    newer_ver = _copy("newer-ver", kind="task-data", version="5.2.10", **same)
    assert choose_canonical([older_ver, newer_ver]).id == "newer-ver"

    # No version on either: the version inside processing:software is the fallback.
    soft_low = _copy("soft-low", kind="tasks", software="5.2.8", **same)
    soft_high = _copy("soft-high", kind="task-data", software="5.2.10", **same)
    assert choose_canonical([soft_low, soft_high]).id == "soft-high"

    # Version tied (both absent): newer datetime, then more products (CPHD counts),
    # then a task-data href, then the lowest id.
    early = _copy(
        "early", kind="task-data", created="2026-01-01T00:00:00Z", dt="2024-06-01T00:00:00Z"
    )
    late = _copy("late", kind="tasks", created="2026-01-01T00:00:00Z", dt="2024-06-01T00:00:30Z")
    assert choose_canonical([early, late]).id == "late"

    plain = _copy("plain", kind="task-data", products=("GEC",), **same)
    richer = _copy("richer", kind="tasks", products=("GEC", "CPHD"), **same)
    assert choose_canonical([plain, richer]).id == "richer"

    tasks_only = _copy("tasks-only", kind="tasks", products=("GEC",), **same)
    task_data = _copy("task-data-only", kind="task-data", products=("GEC",), **same)
    assert choose_canonical([tasks_only, task_data]).id == "task-data-only"

    high_id = _copy("m-id", kind="tasks", products=("GEC",), **same)
    low_id = _copy("a-id", kind="tasks", products=("GEC",), **same)
    assert choose_canonical([high_id, low_id]).id == "a-id"

    # A missing created loses to any present one.
    missing = _copy("missing-created", kind="task-data", dt="2024-06-01T00:00:30Z")
    present = _copy(
        "has-created",
        kind="tasks",
        created="2020-01-01T00:00:00Z",
        dt="2024-06-01T00:00:00Z",
    )
    assert choose_canonical([missing, present]).id == "has-created"


def test_sixty_second_window_splits_a_later_imaging_window():
    base = _copy("t0", dt="2024-06-01T00:00:00Z")
    at_60 = _copy("t60", kind="tasks", dt="2024-06-01T00:01:00Z")
    assert len(duplicate_groups([base, at_60])) == 1

    at_61 = _copy("t61", kind="tasks", dt="2024-06-01T00:01:01Z")
    assert duplicate_groups([base, at_61]) == []

    # Diameter is measured from the cluster start, so 0s + 50s stay together
    # and 100s is its own window.
    mid = _copy("t50", kind="tasks", dt="2024-06-01T00:00:50Z")
    far = _copy("t100", kind="other", dt="2024-06-01T00:01:40Z")
    groups = duplicate_groups([far, base, mid])
    assert len(groups) == 1
    assert {item.id for item in groups[0]} == {"t0", "t50"}


def test_undated_items_are_not_grouped_and_singletons_stay_canonical():
    dated = _copy("dated")
    undated = _copy("undated", kind="tasks", dt=None)
    assert duplicate_groups([dated, undated]) == []
    summary = annotate_republished([dated, undated])
    assert summary == {
        "groups": 0,
        "extra_items": 0,
        "unique_captures": 2,
        "low_confidence_groups": 0,
    }
    for item in (dated, undated):
        assert item.properties["umbra-py:canonical"] is True
        assert "umbra-py:canonical_id" not in item.properties
        assert "umbra-py:duplicate_ids" not in item.properties
        assert "umbra-py:duplicate_confidence" not in item.properties
        assert [link for link in item.raw["links"] if link["rel"] in {"canonical", "related"}] == []


def test_confidence_and_links():
    high_a = _copy("task-data-copy", kind="task-data", created="2026-01-01T00:00:00Z")
    high_b = _copy("tasks-copy", kind="tasks", created="2026-02-01T00:00:00Z")
    summary = annotate_republished([high_a, high_b])
    assert summary["groups"] == 1
    assert summary["low_confidence_groups"] == 0
    assert high_b.properties["umbra-py:canonical"] is True
    assert high_a.properties["umbra-py:canonical"] is False
    assert high_a.properties["umbra-py:canonical_id"] == "tasks-copy"
    assert high_a.properties["umbra-py:duplicate_ids"] == ["tasks-copy"]
    assert high_b.properties["umbra-py:duplicate_ids"] == ["task-data-copy"]
    assert high_a.properties["umbra-py:duplicate_confidence"] == "high"
    canonical_links = [link for link in high_a.raw["links"] if link["rel"] == "canonical"]
    assert canonical_links == [
        {
            "rel": "canonical",
            "href": high_b.href,
            "type": "application/json",
            "title": "tasks-copy",
        }
    ]
    assert [link for link in high_b.raw["links"] if link["rel"] == "canonical"] == []
    assert [link["rel"] for link in high_b.raw["links"] if link["rel"] == "related"] == ["related"]

    both = [
        _copy("td-1", kind="task-data", task="t", collect="c"),
        _copy("td-2", kind="task-data", task="t", collect="c"),
    ]
    annotate_republished(both)
    assert both[0].properties["umbra-py:duplicate_confidence"] == "low"

    triple = [
        _copy("one", kind="task-data", task="t2", collect="c2"),
        _copy("two", kind="tasks", task="t2", collect="c2"),
        _copy("three", kind="tasks", task="t2", collect="c2"),
    ]
    annotate_republished(triple)
    assert {item.properties["umbra-py:duplicate_confidence"] for item in triple} == {"low"}
    assert all(len(item.properties["umbra-py:duplicate_ids"]) == 2 for item in triple)


def test_sanitize_links_without_dropping_rows(tmp_path):
    pair = [
        _copy("keep-me", kind="tasks", created="2026-02-01T00:00:00Z"),
        _copy("also-keep", kind="task-data", created="2026-01-01T00:00:00Z"),
    ]
    alone = _copy("solo", task="other-task", collect="other-collect")
    idx = _index(tmp_path, *pair, alone)
    report = sanitize_index(idx, now=NOW)
    assert len(idx) == 3
    assert report["duplicates_dropped"] == 0
    assert report["republished_captures"] == {
        "groups": 1,
        "extra_items": 1,
        "unique_captures": 2,
        "low_confidence_groups": 0,
    }
    assert idx.unique_captures() == 2
    ids = {item.id: item for item in idx.search()}
    assert ids["keep-me"].properties["umbra-py:canonical"] is True
    assert ids["also-keep"].properties["umbra-py:canonical"] is False
    assert ids["solo"].properties["umbra-py:canonical"] is True
    kept = {item.id for item in idx.search(canonical=True)}
    assert kept == {"keep-me", "solo"}
    assert {item.id for item in idx.search(canonical=False)} == {"also-keep"}
    idx.close()


def test_search_api_and_counts(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    pair = [
        _copy("canon", kind="tasks", created="2026-02-01T00:00:00Z"),
        _copy("copy", kind="task-data", created="2026-01-01T00:00:00Z"),
    ]
    idx = _index(tmp_path, *pair)
    sanitize_index(idx, now=NOW)
    idx.close()

    client = TestClient(build_app(tmp_path / "catalog.db"))
    health = client.get("/healthz").json()
    assert health["items"] == 2
    assert health["unique_captures"] == 1
    coll = client.get("/collections/umbra-open-data").json()
    assert coll["umbra-py:items"] == 2
    assert coll["umbra-py:unique_captures"] == 1

    everything = client.get("/search").json()
    assert {feature["id"] for feature in everything["features"]} == {"canon", "copy"}
    only = client.get("/search", params={"canonical": "true"}).json()
    assert [feature["id"] for feature in only["features"]] == ["canon"]
    others = client.get("/search", params={"canonical": "false"}).json()
    assert [feature["id"] for feature in others["features"]] == ["copy"]
    assert client.get("/search", params={"canonical": "maybe"}).status_code == 400

    posted = client.post(
        "/search",
        json={"query": {"umbra-py:canonical": {"eq": True}}},
    ).json()
    assert [feature["id"] for feature in posted["features"]] == ["canon"]
    overridden = client.post(
        "/search",
        json={"canonical": False, "query": {"umbra-py:canonical": {"eq": True}}},
    ).json()
    assert [feature["id"] for feature in overridden["features"]] == ["copy"]

    feature = client.get("/collections/umbra-open-data/items/copy").json()
    assert feature["properties"]["umbra-py:canonical"] is False
    rels = {link["rel"] for link in feature["links"]}
    assert {"self", "root", "parent", "collection", "canonical", "related"} <= rels


def test_item_to_stac_keeps_duplicate_links_only():
    item = _copy("copy", kind="task-data", created="2026-01-01T00:00:00Z")
    winner = _copy("canon", kind="tasks", created="2026-02-01T00:00:00Z")
    annotate_republished([item, winner])
    item.raw["links"].append({"rel": "self", "href": "https://example.com/old"})
    feature = item_to_stac(item, "http://localhost:8000")
    rels = [link["rel"] for link in feature["links"]]
    assert rels[:4] == ["self", "root", "parent", "collection"]
    assert "canonical" in rels and "related" in rels
    assert feature["links"][0]["href"].endswith("/items/copy")


def test_parse_query_reads_canonical_eq():
    parsed = parse_query({"umbra-py:canonical": {"eq": False}})
    assert parsed.canonical is False
    assert parse_query({}).canonical is None


def test_mcp_search_catalog_canonical_filter(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    mcp = pytest.importorskip("umbra_py.mcp_server")
    pair = [
        _copy("canon", kind="tasks", created="2026-02-01T00:00:00Z"),
        _copy("copy", kind="task-data", created="2026-01-01T00:00:00Z"),
    ]
    idx = _index(tmp_path, *pair)
    sanitize_index(idx, now=NOW)
    idx.close()
    monkeypatch.setattr(mcp, "default_index_path", lambda: tmp_path / "catalog.db")
    monkeypatch.setattr(mcp, "_canopy_token", lambda: None)

    both = mcp.search_catalog(local=True, limit=10)
    assert {card["id"] for card in both["items"]} == {"canon", "copy"}
    only = mcp.search_catalog(local=True, limit=10, canonical=True)
    assert [card["id"] for card in only["items"]] == ["canon"]
    assert "canonical=True" in mcp.build_server().instructions


def test_v6_index_gains_canonical_defaulting_true(tmp_path):
    path = tmp_path / "v6.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE items (
            href TEXT PRIMARY KEY, id TEXT NOT NULL, task TEXT, datetime TEXT,
            acq_date TEXT, min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
            doc TEXT NOT NULL, place TEXT, thumbnail BLOB,
            thumbnail_asset TEXT, thumbnail_size INTEGER,
            datetime_suspect INTEGER NOT NULL DEFAULT 0,
            created TEXT, published TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT INTO items (href, id, doc) VALUES (?, ?, ?)",
        ("h", "old", '{"id": "old", "properties": {}, "assets": {}}'),
    )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    with CatalogIndex(path) as idx:
        flag = idx._conn.execute("SELECT canonical FROM items").fetchone()[0]
        assert flag == 1
        assert idx.unique_captures() == 1


def test_coverage_report_counts_unique_captures(tmp_path):
    from umbra_py.parity import coverage_report, render_markdown

    idx = _index(
        tmp_path,
        _copy("canon", kind="tasks", created="2026-02-01T00:00:00Z"),
        _copy("copy", kind="task-data", created="2026-01-01T00:00:00Z"),
    )
    sanitize_index(idx, now=NOW)
    report = coverage_report(idx, [])
    assert report["index"]["rows"] == 2
    assert report["index"]["unique_captures"] == 1
    assert report["validation"]["republished_captures"]["groups"] == 1
    rendered = render_markdown(report)
    assert "1 unique captures" in rendered
    assert "1 group(s)" in rendered
    idx.close()


def test_index_info_reports_unique_captures(tmp_path):
    idx = _index(tmp_path, _copy("solo", task="t", collect="c"))
    idx.close()
    result = CliRunner().invoke(
        cli, ["index", "info", "--db", str(tmp_path / "catalog.db"), "--json"]
    )
    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    assert info["items"] == 1
    assert info["unique_captures"] == 1


def test_geoparquet_carries_canonical_columns(tmp_path):
    pytest.importorskip("stac_geoparquet")
    pq = pytest.importorskip("pyarrow.parquet")
    from umbra_py.export import export_geoparquet

    items = [
        _copy("canon", kind="tasks", created="2026-02-01T00:00:00Z"),
        _copy("copy", kind="task-data", created="2026-01-01T00:00:00Z"),
    ]
    annotate_republished(items)
    out = tmp_path / "out.parquet"
    assert export_geoparquet(items, out) == 2
    schema = pq.read_schema(out)
    names = set(schema.names)
    assert "umbra-py:canonical" in names
    assert "umbra-py:canonical_id" in names
    assert "umbra-py:duplicate_ids" in names
    assert "umbra-py:duplicate_confidence" in names
