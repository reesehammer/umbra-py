"""Offline tests for the repeat-imaged-site discovery layer.

:mod:`umbra_py.coverage` ranks the archive's most repeat-imaged sites and
summarises each one's coverage (the discovery step before ``umbra change`` /
``stack``); ``umbra sites`` is its CLI front door. Both are exercised here with
hand-built items and a monkeypatched gather -- no network, no renderer, no model.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from umbra_py.coverage import SiteCoverage, rank_site_coverage, site_coverage
from umbra_py.models import UmbraItem


def _pass(task: str, day: int, *, place: str | None = None, bbox=None, pols=None, assets=None):
    """One acquisition of ``task`` on 2024-01-``day`` (mirrors the showcase
    test helper: what matters is the task, the date and -- here -- the footprint,
    polarizations and assets the coverage summary reads)."""
    props: dict = {"datetime": f"2024-01-{day:02d}T00:00:00Z"}
    if pols is not None:
        props["sar:polarizations"] = pols
    item = UmbraItem.from_dict(
        {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": f"{task}-{day}",
            "bbox": list(bbox) if bbox else [-110.0, 39.0, -109.9, 39.1],
            "geometry": None,
            "properties": props,
            "assets": assets or {},
        },
        href=f"https://x.s3.amazonaws.com/sar-data/tasks/{task}/t/{day}/i.json",
    )
    item.place = place
    return item


# --------------------------------------------------------------------------- #
# site_coverage: one site -> one summary
# --------------------------------------------------------------------------- #
def test_site_coverage_summarises_passes_dates_and_cadence():
    site = site_coverage("Alpha", [_pass("Alpha", d) for d in (10, 1, 4)])
    assert site.passes == 3
    # Ordered oldest-first regardless of input order.
    assert site.first == "2024-01-01"
    assert site.last == "2024-01-10"
    assert site.span_days == 9
    # Gaps are 3 and 6 days: shortest 3, median 4.5.
    assert site.min_revisit_days == 3.0
    assert site.median_revisit_days == 4.5
    assert site.hrefs[0].endswith("/t/1/i.json")
    assert site.hrefs[-1].endswith("/t/10/i.json")


def test_site_coverage_label_prefers_place_then_task():
    task_only = site_coverage("Alpha", [_pass("Alpha", 1)])
    assert task_only.label == "Alpha"
    labelled = site_coverage("Alpha", [_pass("Alpha", 1), _pass("Alpha", 2, place="Ogden, Utah")])
    assert labelled.label == "Ogden, Utah"
    # An explicit label wins (how rank_site_coverage keeps FeaturedSite.label single-source).
    assert site_coverage("Alpha", [_pass("Alpha", 1)], label="Given").label == "Given"


def test_site_coverage_unions_footprints_and_collects_facets():
    site = site_coverage(
        "Alpha",
        [
            _pass("Alpha", 1, bbox=(0, 0, 1, 1), pols=["VV"], assets={"GEC": {"href": "g.tif"}}),
            _pass("Alpha", 2, bbox=(2, -1, 3, 4), pols=["HH", "VV"]),
        ],
    )
    assert site.bbox == (0, -1, 3, 4)
    assert site.polarizations == ("HH", "VV")
    assert site.products == ("GEC",)


def test_site_coverage_single_pass_has_no_revisit():
    site = site_coverage("Alpha", [_pass("Alpha", 1)])
    assert site.passes == 1
    assert site.span_days is None
    assert site.min_revisit_days is None
    assert site.median_revisit_days is None


def test_site_coverage_undated_pass_rides_along_without_breaking_cadence():
    undated = _pass("Alpha", 1)
    undated.properties.pop("datetime")
    site = site_coverage("Alpha", [undated, _pass("Alpha", 2), _pass("Alpha", 5)])
    # Three passes carried, but cadence is measured only over the two dated ones.
    assert site.passes == 3
    assert site.min_revisit_days == 3.0
    assert len(site.hrefs) == 3


def test_to_dict_is_json_ready():
    site = site_coverage(
        "Alpha", [_pass("Alpha", 1, bbox=(0, 0, 1, 1)), _pass("Alpha", 3, bbox=(2, 2, 3, 3))]
    )
    payload = json.loads(json.dumps(site.to_dict()))
    assert payload["task"] == "Alpha"
    assert payload["passes"] == 2
    assert payload["bbox"] == [0.0, 0.0, 3.0, 3.0]
    assert payload["hrefs"] and all(h.startswith("http") for h in payload["hrefs"])


# --------------------------------------------------------------------------- #
# rank_site_coverage: pool -> ranked summaries (reuses select_featured_sites)
# --------------------------------------------------------------------------- #
def test_rank_orders_by_pass_count_then_task():
    pool = [
        *[_pass("Bravo", d) for d in (1, 2, 3)],
        *[_pass("Alpha", d) for d in (4, 5, 6)],
        *[_pass("Delta", d) for d in (7, 8)],
        _pass("Echo", 9),  # single pass -- below min_passes
    ]
    ranked = rank_site_coverage(pool, top=6)
    assert [s.label for s in ranked] == ["Alpha", "Bravo", "Delta"]
    assert all(isinstance(s, SiteCoverage) for s in ranked)


def test_rank_respects_top_and_min_passes():
    pool = [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]
    assert [s.label for s in rank_site_coverage(pool, top=1)] == ["Alpha"]
    assert [s.label for s in rank_site_coverage(pool, min_passes=3)] == ["Alpha"]
    assert rank_site_coverage([]) == []


# --------------------------------------------------------------------------- #
# umbra sites CLI
# --------------------------------------------------------------------------- #
def _patch_gather(monkeypatch, pool):
    captured: dict = {}

    def _fake_gather(**kwargs):
        captured.update(kwargs)
        return pool

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", _fake_gather)
    return captured


def test_sites_cli_human_output(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Alpha", d, place="Ogden") for d in (1, 4, 8)],
        *[_pass("Bravo", d) for d in (2, 3)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites"])
    assert result.exit_code == 0, result.output
    assert "Ogden" in result.output  # baked place label wins
    assert "task     : Alpha" in result.output  # codename shown when it differs
    assert "passes   : 3" in result.output
    assert "2 site(s), best-covered first." in result.output


def test_sites_cli_json_carries_pass_urls(monkeypatch):
    from umbra_py.cli import cli

    _patch_gather(monkeypatch, [_pass("Alpha", d) for d in (1, 5)])
    result = CliRunner().invoke(cli, ["sites", "--json"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["passes"] == 2
    assert rows[0]["min_revisit_days"] == 4.0
    assert len(rows[0]["hrefs"]) == 2


def test_sites_cli_forwards_gather_kwargs(monkeypatch):
    """The parity suites check --area/--fuzzy/--intersects; this pins the rest of
    the search (dates, product, pool limit) reaching the live backend too."""
    from umbra_py.cli import cli

    captured = _patch_gather(monkeypatch, [])
    result = CliRunner().invoke(
        cli,
        ["sites", "--start", "2024-01-01", "--product", "GEC", "--limit", "42"],
    )
    assert result.exit_code == 0, result.output
    assert captured["start"] == "2024-01-01"
    assert captured["product_types"] == ["GEC"]
    assert captured["limit"] == 42


def test_sites_cli_empty_pool_explains(monkeypatch):
    from umbra_py.cli import cli

    _patch_gather(monkeypatch, [_pass("Alpha", 1)])  # single pass, below min_passes
    result = CliRunner().invoke(cli, ["sites"])
    assert result.exit_code == 0, result.output
    assert "has 2+" in result.output
    assert "pool of 1 acquisition(s)" in result.output


def _build_sites_index(tmp_path, pool):
    """Persist a pool of hand-built passes into a real CatalogIndex on disk, so
    the ``--local`` path (which reads SQL, not a patched gather) can be exercised.
    """
    from umbra_py.index import CatalogIndex

    with CatalogIndex(tmp_path / "catalog.db") as idx:
        for item in pool:
            idx.add(item)
    return tmp_path / "catalog.db"


def test_sites_cli_local_ranks_whole_index_not_a_capped_pool(tmp_path):
    """--local ranks the whole index directly: a deeply-imaged site is found even
    when its passes would fall outside a --limit-sized pool, and --limit is not
    used on this path (a GROUP BY task, so depth is never capped)."""
    from umbra_py.cli import cli

    # Deep is the alphabetically-last task, so a limited pool ordered by
    # (task, acq_date) would admit the shallow sites first and cap Deep out.
    pool = [
        *[_pass("Aaa", d) for d in (1, 2)],
        *[_pass("Bbb", d) for d in (3, 4)],
        *[_pass("Zzz", d) for d in (5, 6, 7, 8)],
    ]
    db = _build_sites_index(tmp_path, pool)
    result = CliRunner().invoke(cli, ["sites", "--index-db", str(db), "--limit", "3", "--json"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    # All three sites ranked (limit ignored), deepest first.
    assert [r["task"] for r in rows] == ["Zzz", "Aaa", "Bbb"]
    assert rows[0]["passes"] == 4


def test_sites_cli_local_missing_index_errors(tmp_path):
    from umbra_py.cli import cli

    result = CliRunner().invoke(cli, ["sites", "--index-db", str(tmp_path / "nope.db")])
    assert result.exit_code != 0
    assert "No index at" in result.output


def test_sites_cli_local_empty_index_explains(tmp_path):
    from umbra_py.cli import cli

    db = _build_sites_index(tmp_path, [_pass("Alpha", 1)])  # single pass
    result = CliRunner().invoke(cli, ["sites", "--local", "--index-db", str(db)])
    assert result.exit_code == 0, result.output
    assert "No site in the local index has 2+ passes" in result.output


def test_sites_cli_local_forwards_filters_to_rank_sites(tmp_path, monkeypatch):
    """The local path threads geography / task-name / product filters (and --top /
    --min-passes) down to CatalogIndex.rank_sites -- the deep-path counterpart of
    the parity suites' forwarding check for the live gather."""
    from umbra_py.cli import cli
    from umbra_py.index import CatalogIndex

    db = _build_sites_index(tmp_path, [_pass("Alpha", d) for d in (1, 2)])
    captured: dict = {}

    def _fake_rank(self, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(CatalogIndex, "rank_sites", _fake_rank)
    result = CliRunner().invoke(
        cli,
        [
            "sites",
            "--index-db",
            str(db),
            "--area",
            "Centerfield",
            "--fuzzy",
            "--product",
            "GEC",
            "--top",
            "5",
            "--min-passes",
            "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["area"] == "Centerfield"
    assert captured["fuzzy"] is True
    assert captured["product_types"] == ["GEC"]
    assert captured["top"] == 5
    assert captured["min_passes"] == 3


def test_sites_cli_token_conflicts_with_local(monkeypatch):
    from umbra_py.cli import cli

    result = CliRunner().invoke(cli, ["sites", "--local", "--token", "abc"])
    assert result.exit_code != 0
    assert "--token" in result.output
