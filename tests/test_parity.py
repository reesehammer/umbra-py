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
    assert "Overall: 2 of 4" in parity.render_markdown(report)


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
