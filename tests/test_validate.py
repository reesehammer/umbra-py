"""Publish-rule validation for the catalog index (PRD P1-2)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from umbra_py.cli import cli
from umbra_py.exceptions import IndexValidationError
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem
from umbra_py.validate import (
    FUTURE_SLACK,
    asset_reasons,
    collect_violations,
    datetime_reasons,
    geometry_reason,
    item_reasons,
    recheck_report,
    sanitize_index,
    stored_validation,
)

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
}


def _item(
    item_id: str = "a",
    *,
    dt: str | None = "2024-06-01T00:00:00Z",
    href: str | None = None,
    assets: dict | None = None,
    updated: str | None = None,
    geometry: dict | None = SQUARE,
    links: list | None = None,
) -> UmbraItem:
    href = href or f"https://example.com/{item_id}.json"
    if assets is None:
        assets = {
            "GEC": {
                "href": f"https://example.com/{item_id}_GEC.tif",
                "type": "image/tiff; application=geotiff",
            }
        }
    props: dict = {}
    if dt is not None:
        props["datetime"] = dt
    if updated is not None:
        props["updated"] = updated
    doc: dict = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "properties": props,
        "bbox": [0, 0, 1, 1],
        "geometry": geometry,
        "assets": assets,
    }
    if links is not None:
        doc["links"] = links
    return UmbraItem.from_dict(doc, href=href)


def _index(tmp_path, *items: UmbraItem) -> CatalogIndex:
    tmp_path.mkdir(parents=True, exist_ok=True)
    idx = CatalogIndex(tmp_path / "catalog.db")
    for item in items:
        idx.add(item)
    idx.commit()
    return idx


def test_sanitize_keeps_timestamp_columns_on_a_valid_row(tmp_path):
    """Quarantine still drops a bad sibling and leaves created/published put."""
    kept = _item("kept")
    kept.properties["created"] = "2024-06-03T00:00:00Z"
    kept.published = datetime(2024, 6, 4, tzinfo=timezone.utc)
    future = _item("future", dt="2036-04-01T00:00:00Z")
    with _index(tmp_path, kept, future) as idx:
        report = sanitize_index(idx, now=NOW)
        assert report["kept"] == 1
        assert report["quarantined_count"] == 1
        got = idx.get("kept")
        assert got is not None
        assert got.properties["created"] == "2024-06-03T00:00:00Z"
        assert got.published == datetime(2024, 6, 4, tzinfo=timezone.utc)
        assert idx.get("future") is None


def test_datetime_before_2018_is_quarantined():
    early = _item(dt="2017-12-31T23:59:59Z")
    on_epoch = _item(dt="2018-01-01T00:00:00Z")
    assert datetime_reasons(early, now=NOW) == ["datetime before 2018-01-01"]
    assert datetime_reasons(on_epoch, now=NOW) == []


def test_datetime_after_build_plus_one_day_is_quarantined():
    cutoff = NOW + FUTURE_SLACK
    on_cutoff = _item(dt=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
    after = _item(dt=(cutoff + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    future = _item(dt="2035-06-01T00:00:00Z")
    assert datetime_reasons(on_cutoff, now=NOW) == []
    assert datetime_reasons(after, now=NOW) == ["datetime in the future"]
    assert datetime_reasons(future, now=NOW) == ["datetime in the future"]


def test_acquisition_directory_date_is_checked_too():
    """A 2035 folder is dated in the future even when STAC datetime is fine."""
    href = (
        "https://s3.example.com/sar-data/tasks/Site/"
        "2036-01-02-00-00-00_UMBRA-04/2036-01-02-00-00-00_UMBRA-04.stac.v2.json"
    )
    assert datetime_reasons(_item(href=href, dt="2024-06-01T00:00:00Z"), now=NOW) == [
        "datetime in the future"
    ]
    old = (
        "https://s3.example.com/sar-data/tasks/Site/"
        "2017-05-01-00-00-00_UMBRA-04/2017-05-01-00-00-00_UMBRA-04.stac.v2.json"
    )
    assert datetime_reasons(_item(href=old), now=NOW) == ["datetime before 2018-01-01"]


def test_missing_datetime_is_not_a_date_violation():
    assert datetime_reasons(_item(dt=None), now=NOW) == []


def test_geometry_rules():
    assert geometry_reason(None) == "missing geometry"
    assert geometry_reason({}) == "invalid geometry"
    assert geometry_reason({"type": "Polygon", "coordinates": []}) == "empty geometry"
    assert (
        geometry_reason({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [2, 0], [0, 0]]]})
        == "empty geometry"
    )
    assert (
        geometry_reason({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]})
        == "invalid geometry"
    )
    # Unequal bowtie: the lobes do not cancel, and the edges cross.
    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [3, 1], [0, 1], [1, 0], [0, 0]]],
    }
    assert geometry_reason(bowtie) == "invalid geometry"
    assert geometry_reason(SQUARE) is None
    solid = {
        "type": "Polygon",
        "coordinates": [[[0, 0, 5], [1, 0, 5], [1, 1, 6], [0, 1, 6], [0, 0, 5]]],
    }
    assert geometry_reason(solid) is None
    assert geometry_reason({"type": "Point", "coordinates": [10, 20]}) is None
    assert geometry_reason({"type": "Point", "coordinates": []}) == "empty geometry"
    nan_ring = {"type": "Polygon", "coordinates": [[[0, 0], [1, float("nan")], [1, 1], [0, 0]]]}
    assert geometry_reason(nan_ring) == "invalid geometry"


def test_asset_rules():
    assert asset_reasons(_item()) == []
    assert asset_reasons(_item(assets={"GEC": {"href": ""}})) == ["no public https asset href"]
    assert asset_reasons(_item(assets={"GEC": {"href": "http://example.com/a.tif"}})) == [
        "no public https asset href"
    ]
    private = _item(
        assets={
            "GEC": {"href": "https://example.com/a.tif"},
            "SICD": {"href": "s3://prod-prod-processed-sar-data/2024/a.nitf"},
        }
    )
    assert asset_reasons(private) == ["private prod-prod-processed href"]
    only_private = _item(assets={"GEC": {"href": "s3://prod-prod-processed-sar-data/2024/a.tif"}})
    assert asset_reasons(only_private) == [
        "no public https asset href",
        "private prod-prod-processed href",
    ]


def test_private_href_in_a_link_is_quarantined():
    item = _item(links=[{"rel": "via", "href": "s3://prod-prod-processed-sar-data/secret.json"}])
    assert "private prod-prod-processed href" in item_reasons(item, now=NOW)


def test_one_item_can_carry_several_reasons():
    item = _item(dt="2035-01-01T00:00:00Z", geometry=None, assets={"GEC": {"href": ""}})
    assert item_reasons(item, now=NOW) == [
        "datetime in the future",
        "missing geometry",
        "no public https asset href",
    ]


def test_dedupe_keeps_latest_updated_then_most_assets_then_href(tmp_path):
    older = _item("same", href="https://example.com/a.json", updated="2024-01-01T00:00:00Z")
    newer = _item("same", href="https://example.com/b.json", updated="2025-06-01T00:00:00Z")
    with _index(tmp_path / "updated", older, newer) as idx:
        report = sanitize_index(idx, now=NOW)
        assert [item.id for item in idx.search()] == ["same"]
        assert idx.get("same").href == newer.href  # type: ignore[union-attr]
    assert report["duplicates_dropped"] == 1
    assert report["duplicates"][0]["href"] == older.href
    assert report["duplicates"][0]["kept_href"] == newer.href

    # No `updated`: the later datetime wins.
    early = _item("same", href="https://example.com/a.json", dt="2020-01-01T00:00:00Z")
    late = _item("same", href="https://example.com/b.json", dt="2024-01-01T00:00:00Z")
    with _index(tmp_path / "datetime", early, late) as idx:
        sanitize_index(idx, now=NOW)
        assert idx.get("same").href == late.href  # type: ignore[union-attr]

    # Same freshness: the record with more assets wins.
    one = _item("same", href="https://example.com/a.json", updated="2024-01-01T00:00:00Z")
    two = _item(
        "same",
        href="https://example.com/b.json",
        updated="2024-01-01T00:00:00Z",
        assets={
            "GEC": {"href": "https://example.com/b_GEC.tif"},
            "SICD": {"href": "https://example.com/b_SICD.nitf"},
        },
    )
    with _index(tmp_path / "assets", one, two) as idx:
        sanitize_index(idx, now=NOW)
        assert idx.get("same").href == two.href  # type: ignore[union-attr]

    # Still tied: the lowest href, independent of insertion order.
    first = _item("same", href="https://example.com/b.json", updated="2024-01-01T00:00:00Z")
    second = _item("same", href="https://example.com/a.json", updated="2024-01-01T00:00:00Z")
    with _index(tmp_path / "href", first, second) as idx:
        report = sanitize_index(idx, now=NOW)
        assert idx.get("same").href == second.href  # type: ignore[union-attr]
    assert report["duplicates"][0]["kept_href"] == second.href


def test_a_quarantined_duplicate_cannot_win(tmp_path):
    """The future-dated copy has a later updated and more assets; it is still dropped."""
    good = _item("same", href="https://example.com/a.json", dt="2024-01-01T00:00:00Z")
    future = _item(
        "same",
        href="https://example.com/b.json",
        dt="2035-01-01T00:00:00Z",
        updated="2035-01-01T00:00:00Z",
        assets={
            "GEC": {"href": "https://example.com/b_GEC.tif"},
            "SICD": {"href": "https://example.com/b_SICD.nitf"},
            "CPHD": {"href": "https://example.com/b.cphd"},
        },
    )
    with _index(tmp_path, future, good) as idx:
        report = sanitize_index(idx, now=NOW)
        assert idx.get("same").href == good.href  # type: ignore[union-attr]
    assert report["quarantined_count"] == 1
    assert report["duplicates_dropped"] == 0
    assert report["quarantined"][0]["reasons"] == ["datetime in the future"]
    assert report["violations"] == []


def test_build_quarantines_without_raising(tmp_path):
    good = _item("good")
    future = _item("future", dt="2036-01-01T00:00:00Z")

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([good, future])

    with CatalogIndex(tmp_path / "catalog.db") as idx:
        written = idx.build(FakeCatalog())
        assert written == 2
        assert {item.id for item in idx.search()} == {"good"}
        report = stored_validation(idx)
    assert report is not None
    assert report["quarantined_count"] == 1
    assert report["violations"] == []


def test_recheck_of_a_dirty_index_is_a_violation(tmp_path):
    with _index(tmp_path, _item("bad", dt="2035-03-01T00:00:00Z")) as idx:
        violations = collect_violations(idx.search(), now=NOW)
        assert violations[0]["reasons"] == ["datetime in the future"]
        report = recheck_report(idx, now=NOW)
    assert report["recheck"]["index_violations"]
    from umbra_py.validate import assert_recheck_clean

    with pytest.raises(IndexValidationError, match="datetime in the future"):
        assert_recheck_clean(report)


def test_sanitize_raises_when_a_violation_survives(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "umbra_py.validate.collect_violations",
        lambda items, *, now: [
            {"id": "still-bad", "href": "https://example.com/x.json", "reasons": ["duplicate id"]}
        ],
    )
    with _index(tmp_path, _item()) as idx:
        with pytest.raises(IndexValidationError, match="still-bad"):
            sanitize_index(idx, now=NOW)


def test_cli_validate_fails_loudly_and_writes_the_report(tmp_path):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_item("bad", geometry=None))
        idx.commit()
    out = tmp_path / "index-validation.json"
    result = CliRunner().invoke(cli, ["index", "validate", "--db", str(db), "--json", str(out)])
    assert result.exit_code != 0, result.output
    assert "missing geometry" in result.output
    payload = json.loads(out.read_text())
    assert payload["recheck"]["index_violations"][0]["reasons"] == ["missing geometry"]


def test_cli_validate_passes_after_sanitize(tmp_path):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_item("good"))
        idx.add(_item("bad", dt="2035-01-01T00:00:00Z"))
        idx.commit()
        sanitize_index(idx, now=NOW)
    result = CliRunner().invoke(cli, ["index", "validate", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Quarantined 1 item(s); dropped 0 duplicate id(s); 0 index violation(s) remain." in (
        result.output
    )


def test_cli_build_quarantines_and_keeps_going(tmp_path, monkeypatch):
    from umbra_py import index as index_mod

    good = _item("good")
    future = _item("future", dt="2035-08-01T00:00:00Z")

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([good, future])

    monkeypatch.setattr(index_mod, "UmbraCatalog", lambda *a, **k: FakeCatalog())
    db = str(tmp_path / "catalog.db")
    result = CliRunner().invoke(cli, ["index", "build", "--db", db])
    assert result.exit_code == 0, result.output
    assert "Quarantined 1 item(s); dropped 0 duplicate id(s)." in result.output
    with CatalogIndex(db) as idx:
        assert {item.id for item in idx.search()} == {"good"}


def test_cli_build_fails_loudly_when_a_violation_remains(tmp_path, monkeypatch):
    from umbra_py import index as index_mod

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([_item("good")])

    monkeypatch.setattr(index_mod, "UmbraCatalog", lambda *a, **k: FakeCatalog())
    monkeypatch.setattr(
        "umbra_py.validate.collect_violations",
        lambda items, *, now: [
            {"id": "still-bad", "href": "https://example.com/x.json", "reasons": ["duplicate id"]}
        ],
    )
    result = CliRunner().invoke(cli, ["index", "build", "--db", str(tmp_path / "catalog.db")])
    assert result.exit_code != 0
    assert "still-bad" in result.output
    assert "duplicate id" in result.output


def test_coverage_report_lists_the_quarantine(tmp_path):
    from umbra_py import parity

    with _index(tmp_path, _item("good"), _item("bad", dt="2035-01-01T00:00:00Z")) as idx:
        sanitize_index(idx, now=NOW)
        report = parity.coverage_report(idx, [])
    assert report["validation"]["quarantined_count"] == 1
    assert report["validation"]["quarantined"][0]["id"] == "bad"
    rendered = parity.render_markdown(report)
    assert "## Validation" in rendered
    assert "datetime in the future" in rendered
    assert "| bad |" in rendered


def test_parquet_is_held_to_the_same_rules(tmp_path):
    pytest.importorskip("stac_geoparquet")
    from umbra_py.export import export_geoparquet
    from umbra_py.validate import parquet_violations

    good = _item("good")
    future = _item("future", dt="2035-01-01T00:00:00Z")
    out = tmp_path / "catalog.parquet"
    export_geoparquet([good, future], out)
    violations = parquet_violations(out, now=NOW)
    assert [row["id"] for row in violations] == ["future"]
    assert violations[0]["reasons"] == ["datetime in the future"]

    clean = tmp_path / "clean.parquet"
    export_geoparquet([good], clean)
    assert parquet_violations(clean, now=NOW) == []


def test_recheck_reports_duplicate_ids_without_holding_rows(tmp_path):
    """Duplicate ids are a SQL aggregate; every copy is still a violation."""
    first = _item("same", href="https://example.com/a.json")
    second = _item("same", href="https://example.com/b.json")
    other = _item("other", href="https://example.com/c.json")
    with _index(tmp_path, second, other, first) as idx:
        report = recheck_report(idx, now=NOW)
    violations = report["recheck"]["index_violations"]
    assert [row["href"] for row in violations] == [
        "https://example.com/a.json",
        "https://example.com/b.json",
    ]
    assert all(row["reasons"] == ["duplicate id"] for row in violations)


def test_recheck_streams_a_large_index(tmp_path):
    """A few hundred fat documents must not all be resident during the recheck.

    The published catalog is ~1.7 GB of STAC JSON. Holding every parsed row
    is what the weekly validate step did. This corpus is large enough that
    materialising it exceeds the peak traced below, and small enough to run
    in the offline suite.
    """
    import tracemalloc

    payload = "x" * 32_000
    items = []
    for i in range(250):
        item = _item(f"id-{i:04d}", href=f"https://example.com/{i:04d}.json")
        item.properties["umbra:note"] = payload
        items.append(item)
    items.append(_item("future", dt="2035-04-01T00:00:00Z", href="https://example.com/zz.json"))
    dup_a = _item("dup", href="https://example.com/dup-a.json")
    dup_b = _item("dup", href="https://example.com/dup-b.json")
    with _index(tmp_path, *items, dup_a, dup_b) as idx:
        tracemalloc.start()
        report = recheck_report(idx, now=NOW)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    counts: dict[str, int] = {}
    reasons: dict[str, list] = {}
    for row in report["recheck"]["index_violations"]:
        counts[row["id"]] = counts.get(row["id"], 0) + 1
        reasons[row["id"]] = row["reasons"]
    assert counts == {"future": 1, "dup": 2}
    assert reasons["future"] == ["datetime in the future"]
    assert reasons["dup"] == ["duplicate id"]
    # 250 documents × 32 KB is 8 MB of JSON before Python object overhead.
    # One row in flight stays well under that; the list-everything path cannot.
    assert peak < 6 * 1024 * 1024, peak


def test_parquet_rules_match_a_full_table_read(tmp_path):
    """Batched decoding agrees with stac_table_to_items, including duplicates."""
    pytest.importorskip("stac_geoparquet")
    import pyarrow.parquet as pq
    import stac_geoparquet.arrow as arrow

    from umbra_py.export import export_geoparquet
    from umbra_py.validate import parquet_violations

    bowtie = {
        "type": "Polygon",
        "coordinates": [[[0, 0], [3, 1], [0, 1], [1, 0], [0, 0]]],
    }
    items = []
    for i in range(12):
        key = f"2024-06-01-00-00-{i:02d}_UMBRA-05_GEC.tif"
        items.append(
            _item(
                f"id-{i:02d}",
                href=f"https://example.com/{i:02d}.json",
                assets={key: {"href": f"https://example.com/{key}", "type": "image/tiff"}},
            )
        )
    items.append(_item("same", href="https://example.com/dup-a.json"))
    items.append(_item("same", href="https://example.com/dup-b.json"))
    items.append(_item("future", dt="2036-01-01T00:00:00Z"))
    items.append(_item("bowtie", geometry=bowtie))
    items.append(
        _item(
            "private",
            assets={"GEC": {"href": "s3://prod-prod-processed-sar-data/2024/a.tif"}},
        )
    )
    out = tmp_path / "wide.parquet"
    export_geoparquet(items, out)

    oracle_items = [
        UmbraItem.from_dict(doc) for doc in arrow.stac_table_to_items(pq.read_table(out))
    ]
    oracle = collect_violations(oracle_items, now=NOW)
    got = parquet_violations(out, now=NOW)
    assert [(row["id"], row["reasons"]) for row in got] == [
        (row["id"], row["reasons"]) for row in oracle
    ]
    assert {row["id"] for row in got} >= {"same", "future", "bowtie", "private"}


def test_parquet_validation_reads_ids_and_batches(tmp_path, monkeypatch):
    """The whole table is not read. Only the id column, then fixed-size batches."""
    pytest.importorskip("stac_geoparquet")
    import pyarrow.parquet as pq

    from umbra_py.export import export_geoparquet
    from umbra_py.validate import _PARQUET_BATCH_SIZE, parquet_violations

    out = tmp_path / "catalog.parquet"
    export_geoparquet([_item("good"), _item("also")], out)
    reads: list = []
    batches: list = []
    real_read = pq.read_table
    real_iter = pq.ParquetFile.iter_batches

    def spy_read(source, **kwargs):
        reads.append(kwargs.get("columns"))
        return real_read(source, **kwargs)

    def spy_iter(self, *args, **kwargs):
        batches.append(kwargs)
        return real_iter(self, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", spy_read)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy_iter)
    assert parquet_violations(out, now=NOW) == []
    assert reads == [["id"]]
    assert batches and all(call.get("batch_size") == _PARQUET_BATCH_SIZE for call in batches)
    assert all(call.get("columns") is None for call in batches)


def test_cli_validate_parquet_fails_loudly(tmp_path):
    pytest.importorskip("stac_geoparquet")
    from umbra_py.export import export_geoparquet

    db = tmp_path / "catalog.db"
    good = _item("good")
    with CatalogIndex(db) as idx:
        idx.add(good)
        idx.commit()
        sanitize_index(idx, now=NOW)
    parquet = tmp_path / "catalog.parquet"
    # The export can still carry a row the index no longer holds.
    export_geoparquet([good, _item("future", dt="2036-04-01T00:00:00Z")], parquet)
    result = CliRunner().invoke(
        cli,
        ["index", "validate", "--db", str(db), "--parquet", str(parquet)],
    )
    assert result.exit_code != 0, result.output
    assert "datetime in the future" in result.output
