"""Coverage report (PRD P1-1): bucket acquisitions vs a built index."""

from __future__ import annotations

import json

from click.testing import CliRunner

from umbra_py import parity
from umbra_py.cli import cli
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

BASE = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
S1 = "2024-01-15-10-00-00_UMBRA-04"
S2 = "2023-07-18-02-30-32_UMBRA-04"
S3 = "2023-04-02-07-46-55_UMBRA-05"
S4 = "2024-03-29-03-56-55_UMBRA-07"

KEYS = [
    f"sar-data/task-data/t1/{S1}/{S1}.stac.v2.json",
    f"sar-data/task-data/t1/{S1}/{S1}_GEC.tif",
    f"sar-data/task-data/t1/{S1}/{S1}_CPHD.cphd",
    f"sar-data/task-data/t1/{S1}/{S1}_SICD.nitf",
    f"sar-data/tasks/Panama Canal, Panama/u/{S2}/{S2}_METADATA.json",
    f"sar-data/tasks/Panama Canal, Panama/u/{S2}/{S2}_CPHD.cphd",
    f"sar-data/tasks/Panama Canal, Panama/u/{S2}/{S2}_SICD.nitf",
    f"sar-data/tasks/ad hoc/Aswan/{S3}.tif",
    f"sar-data/tasks/ad hoc/Aswan/{S3}_METADATA.json",
    f"sar-data/tasks/ship/u/{S4}/{S4}_GEC.tif",  # no sidecar at all
    "sar-data/tasks/ship/verified.txt",
]


def _item(item_id: str, href: str, assets: list[str]) -> UmbraItem:
    return UmbraItem.from_dict(
        {
            "id": item_id,
            "bbox": [0, 0, 1, 1],
            "properties": {"datetime": "2024-01-15T10:00:00Z"},
            "assets": {
                f"x_{a}.{'cphd' if a == 'CPHD' else 'nitf' if a == 'SICD' else 'tif'}": {"href": ""}
                for a in assets
            },
        },
        href=href,
    )


def _index(tmp_path):
    idx = CatalogIndex(tmp_path / "catalog.db")
    idx.add(_item("a", f"{BASE}sar-data/task-data/t1/{S1}/{S1}.stac.v2.json", ["GEC", "CPHD"]))
    idx.add(
        _item(
            "b",
            f"{BASE}sar-data/tasks/Panama%20Canal%2C%20Panama/u/{S2}/{S2}_METADATA.json",
            ["CPHD", "SICD"],
        )
    )
    idx.set_meta("built_at", "2026-10-05")
    idx.commit()
    return idx


def test_bucket_acquisitions_groups_layouts():
    acqs = parity.bucket_acquisitions(KEYS)
    layouts = {gid.split("/")[2]: a.layout for gid, a in acqs.items()}
    assert layouts == {
        "t1": "v2",
        "Panama Canal, Panama": "legacy",
        "ad hoc": "legacy",
        "ship": "none",
    }
    flat = next(a for a in acqs.values() if a.task == "ad hoc")
    assert flat.products == {"GEC"}


def test_coverage_report_counts_by_prefix_layout_and_product(tmp_path):
    with _index(tmp_path) as idx:
        report = parity.coverage_report(idx, KEYS)
    assert report["totals"] == {
        "bucket": 4,
        "indexable": 3,
        "indexed": 2,
        "accounted": 0,
        "expected": 3,
        "pct": 50.0,
        "pct_indexable": 66.67,
    }
    assert report["by_prefix"]["sar-data/task-data/"]["pct"] == 100.0
    assert report["by_layout"]["legacy"] == {"bucket": 2, "indexed": 1, "pct": 50.0}
    # SICD exists in the bucket for t1 but the indexed item lacks it
    assert report["by_product"]["sar-data/task-data/"]["SICD"]["indexed"] == 0
    assert report["by_product"]["sar-data/tasks/"]["CPHD"] == {
        "bucket": 1,
        "indexed": 1,
        "pct": 100.0,
    }
    assert [m["group"] for m in report["missing"]] == [f"sar-data/tasks/ad hoc/Aswan/{S3}"]
    assert report["unindexable"] == [f"sar-data/tasks/ship/u/{S4}/"]
    assert report["index"]["distinct_ids"] == 2
    assert report["gate"] == {
        "min_pct_indexable": 99.0,
        "pct_indexable": 66.67,
        "meets": False,
    }
    rendered = parity.render_markdown(report)
    assert "Overall: 2 of 4" in rendered
    assert ">= 99%" in rendered
    assert "is below the bar" in rendered
    assert "No quarantine record" in rendered


def test_cli_coverage_gate(tmp_path):
    _index(tmp_path).close()
    keys = tmp_path / "keys.tsv"
    keys.write_text("\n".join(f"{k}\t1" for k in KEYS) + "\n")
    out = tmp_path / "coverage.json"
    runner = CliRunner()
    ok = runner.invoke(
        cli,
        [
            "index",
            "coverage",
            "--db",
            str(tmp_path / "catalog.db"),
            "--keys",
            str(keys),
            "--json",
            str(out),
        ],
    )
    assert ok.exit_code == 0, ok.output
    assert json.loads(out.read_text())["totals"]["indexed"] == 2
    gated = runner.invoke(
        cli,
        [
            "index",
            "coverage",
            "--db",
            str(tmp_path / "catalog.db"),
            "--keys",
            str(keys),
            "--min-pct",
            "99",
        ],
    )
    assert gated.exit_code != 0
    assert "below the required 99.0%" in gated.output


def _public_item(item_id: str, href: str, *, dt: str, updated: str | None = None) -> UmbraItem:
    props = {"datetime": dt}
    if updated is not None:
        props["updated"] = updated
    return UmbraItem.from_dict(
        {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": item_id,
            "properties": props,
            "bbox": [0, 0, 1, 1],
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
            },
            "assets": {
                "GEC": {
                    "href": href.replace(".stac.v2.json", "_GEC.tif"),
                    "type": "image/tiff; application=geotiff",
                }
            },
        },
        href=href,
    )


def _sidecar_keys(prefix: str) -> list[str]:
    return [prefix + ".stac.v2.json", prefix + "_GEC.tif"]


def test_deduped_and_quarantined_acquisitions_are_not_missing(tmp_path):
    """The 99% gate fails on a real hole, not on rows validation dropped."""
    from datetime import datetime, timezone

    from umbra_py.validate import sanitize_index

    kept_prefix = (
        "sar-data/task-data/task-keep/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04"
    )
    drop_prefix = "open-data/task-drop/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04"
    quar_prefix = (
        "sar-data/task-data/task-quar/2035-01-09-02-11-12_UMBRA-04/2035-01-09-02-11-12_UMBRA-04"
    )
    miss_prefix = (
        "sar-data/task-data/task-miss/2024-02-02-02-02-02_UMBRA-05/2024-02-02-02-02-02_UMBRA-05"
    )
    kept_href = f"{BASE}{kept_prefix}.stac.v2.json"
    drop_href = f"{BASE}{drop_prefix}.stac.v2.json"
    quar_href = f"{BASE}{quar_prefix}.stac.v2.json"
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.add(
            _public_item(
                "same", kept_href, dt="2024-01-15T10:00:00Z", updated="2024-06-02T00:00:00Z"
            )
        )
        idx.add(
            _public_item(
                "same", drop_href, dt="2024-01-15T10:00:00Z", updated="2024-06-01T00:00:00Z"
            )
        )
        idx.add(_public_item("future", quar_href, dt="2035-01-09T02:11:12Z"))
        idx.commit()
        report = sanitize_index(idx, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        assert report["duplicates_dropped"] == 1
        assert report["quarantined_count"] == 1
        keys = [
            *_sidecar_keys(kept_prefix),
            *_sidecar_keys(drop_prefix),
            *_sidecar_keys(quar_prefix),
            *_sidecar_keys(miss_prefix),
        ]
        covered = parity.coverage_report(idx, keys)
    assert covered["totals"]["accounted"] == 2
    assert covered["totals"]["expected"] == 2
    assert covered["totals"]["indexed"] == 1
    assert covered["totals"]["pct_indexable"] == 50.0
    assert covered["missing_count"] == 1
    assert miss_prefix.rsplit("/", 1)[0] + "/" in covered["missing"][0]["group"]
    rendered = parity.render_markdown(covered)
    assert "omitted by dedupe or quarantine" in rendered
    assert "datetime in the future" in rendered
    assert "Duplicates dropped: 1" in rendered
    assert "Quarantined: 1" in rendered
    assert covered["gate"]["meets"] is False

    keys_without_hole = keys[:6]
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        closed = parity.coverage_report(idx, keys_without_hole)
    assert closed["missing_count"] == 0
    assert closed["totals"]["pct_indexable"] == 100.0
    assert closed["totals"]["accounted"] == 2
    assert closed["gate"]["meets"] is True
    assert "meets the bar" in parity.render_markdown(closed)


def test_validation_counts_without_hrefs_do_not_hide_a_real_gap(tmp_path):
    with _index(tmp_path) as idx:
        report = parity.coverage_report(
            idx,
            KEYS,
            validation={
                "duplicates_dropped": 5,
                "quarantined_count": 5,
                "duplicates": [],
                "quarantined": [],
            },
        )
    assert report["totals"]["accounted"] == 0
    assert report["missing_count"] == 1
    assert report["totals"]["pct_indexable"] == 66.67


def test_cli_validation_file_lets_the_gate_ignore_intentional_drops(tmp_path):
    from datetime import datetime, timezone

    from umbra_py.validate import sanitize_index, write_validation_report

    prefix = "sar-data/task-data/only/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04"
    href = f"{BASE}{prefix}.stac.v2.json"
    quar = "sar-data/task-data/quar/2035-01-09-02-11-12_UMBRA-04/2035-01-09-02-11-12_UMBRA-04"
    quar_href = f"{BASE}{quar}.stac.v2.json"
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.add(_public_item("kept", href, dt="2024-01-15T10:00:00Z"))
        idx.add(_public_item("future", quar_href, dt="2035-01-09T02:11:12Z"))
        idx.commit()
        record = sanitize_index(idx, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
    validation = tmp_path / "index-validation.json"
    write_validation_report(record, str(validation), None)
    keys = tmp_path / "keys.txt"
    keys.write_text("\n".join(_sidecar_keys(prefix) + _sidecar_keys(quar)) + "\n")
    result = CliRunner().invoke(
        cli,
        [
            "index",
            "coverage",
            "--db",
            str(tmp_path / "catalog.db"),
            "--keys",
            str(keys),
            "--validation",
            str(validation),
            "--min-pct",
            "99",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "100.0% of the 1 expected" in result.output
