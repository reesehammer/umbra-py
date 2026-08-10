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
    # Gaps are 3 and 6 days: shortest 3, median 4.5, longest 6.
    assert site.min_revisit_days == 3.0
    assert site.median_revisit_days == 4.5
    assert site.max_revisit_days == 6.0
    # Every pass shares one (empty) polarization, so the comparable series is the
    # whole dated range and its cadence triple equals the all-passes one.
    assert site.comparable_min_revisit_days == 3.0
    assert site.comparable_median_revisit_days == 4.5
    assert site.comparable_max_revisit_days == 6.0
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
    assert site.max_revisit_days is None
    assert site.comparable_min_revisit_days is None
    assert site.comparable_median_revisit_days is None
    assert site.comparable_max_revisit_days is None


def test_site_coverage_undated_pass_rides_along_without_breaking_cadence():
    undated = _pass("Alpha", 1)
    undated.properties.pop("datetime")
    site = site_coverage("Alpha", [undated, _pass("Alpha", 2), _pass("Alpha", 5)])
    # Three passes carried, but cadence is measured only over the two dated ones.
    assert site.passes == 3
    assert site.min_revisit_days == 3.0
    assert len(site.hrefs) == 3


def test_max_revisit_separates_a_steady_cadence_from_a_gappy_one():
    # Two sites, same pass count and same median gap, but one has a long hole.
    steady = site_coverage("Steady", [_pass("Steady", d) for d in (1, 7, 13, 19)])
    gappy = site_coverage("Gappy", [_pass("Gappy", d) for d in (1, 7, 13, 28)])
    # Steady gaps 6,6,6; gappy gaps 6,6,15 -> same median, different tail.
    assert steady.median_revisit_days == gappy.median_revisit_days == 6.0
    # The longest gap is what tells them apart.
    assert steady.max_revisit_days == 6.0
    assert gappy.max_revisit_days == 15.0


def test_comparable_passes_is_the_largest_same_polarization_dated_subset():
    # Five dated passes: three VV, two HH. The analysis verbs difference within
    # one polarization, so the deepest comparable series is the three VV passes,
    # not the raw count of five.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 1, pols=["VV"]),
            _pass("Mixed", 3, pols=["VV"]),
            _pass("Mixed", 6, pols=["VV"]),
            _pass("Mixed", 8, pols=["HH"]),
            _pass("Mixed", 10, pols=["HH"]),
        ],
    )
    assert site.passes == 5
    assert site.comparable_passes == 3
    assert site.polarizations == ("HH", "VV")


def test_comparable_passes_equals_the_dated_pass_count_under_one_polarization():
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (1, 2, 3)])
    assert site.comparable_passes == site.passes == 3


def test_comparable_passes_excludes_undated_passes():
    # An undated pass rides along in `passes` and `hrefs` but cannot join a time
    # series, so it is not comparable even when its polarization matches.
    undated = _pass("Alpha", 1, pols=["VV"])
    undated.properties.pop("datetime")
    site = site_coverage(
        "Alpha", [undated, _pass("Alpha", 2, pols=["VV"]), _pass("Alpha", 5, pols=["VV"])]
    )
    assert site.passes == 3
    assert site.comparable_passes == 2


def test_comparable_hrefs_are_the_comparable_group_urls_oldest_first():
    # The same mixed site as above: three VV, two HH. `comparable_hrefs` must be
    # exactly the three VV passes' URLs (the group `comparable_passes` counts),
    # oldest-first, and none of the HH ones -- so a selection handed straight to
    # `umbra stack` cannot trip the mixed-polarization refusal.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 8, pols=["HH"]),
            _pass("Mixed", 6, pols=["VV"]),
            _pass("Mixed", 1, pols=["VV"]),
            _pass("Mixed", 10, pols=["HH"]),
            _pass("Mixed", 3, pols=["VV"]),
        ],
    )
    assert len(site.comparable_hrefs) == site.comparable_passes == 3
    days = [h.rsplit("/t/", 1)[1] for h in site.comparable_hrefs]
    assert days == ["1/i.json", "3/i.json", "6/i.json"]
    assert set(site.comparable_hrefs) <= set(site.hrefs)
    assert all("/t/8/" not in h and "/t/10/" not in h for h in site.comparable_hrefs)


def test_comparable_hrefs_equal_hrefs_under_one_polarization():
    # Every dated pass is comparable, so the usable selection is the whole roster.
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (3, 1, 2)])
    assert site.comparable_hrefs == site.hrefs
    assert len(site.comparable_hrefs) == site.comparable_passes == 3


def test_comparable_hrefs_drop_the_undated_pass_hrefs_keeps():
    # An undated pass rides along in `hrefs` (its URL is still useful to hand off)
    # but cannot join a time series, so it is absent from `comparable_hrefs`.
    undated = _pass("Alpha", 1, pols=["VV"])
    undated.properties.pop("datetime")
    site = site_coverage(
        "Alpha", [undated, _pass("Alpha", 2, pols=["VV"]), _pass("Alpha", 5, pols=["VV"])]
    )
    assert len(site.hrefs) == 3
    assert len(site.comparable_hrefs) == site.comparable_passes == 2
    assert undated.href in site.hrefs
    assert undated.href not in site.comparable_hrefs


def test_comparable_hrefs_empty_when_nothing_dated():
    undated = _pass("None", 1, pols=["VV"])
    undated.properties.pop("datetime")
    site = site_coverage("None", [undated])
    assert site.comparable_passes == 0
    assert site.comparable_hrefs == ()


def test_comparable_span_days_is_measured_over_the_comparable_subset():
    # VV imaged days 5, 10, 15; HH days 1 and 20 bracket them. The whole dated
    # range runs 1 -> 20, but the differenceable (VV) series only spans 5 -> 15,
    # so the analysable window is narrower than the raw span the HH passes stretch.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 1, pols=["HH"]),
            _pass("Mixed", 5, pols=["VV"]),
            _pass("Mixed", 10, pols=["VV"]),
            _pass("Mixed", 15, pols=["VV"]),
            _pass("Mixed", 20, pols=["HH"]),
        ],
    )
    assert site.span_days == 19  # whole dated range, HH included
    assert site.comparable_passes == 3  # the three VV passes
    assert site.comparable_span_days == 10  # 5 -> 15, the analysable window


def test_comparable_span_days_equals_span_under_one_polarization():
    # Every dated pass is comparable, so the analysable window is the whole span.
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (1, 5, 12)])
    assert site.comparable_span_days == site.span_days == 11


def test_comparable_span_days_null_with_fewer_than_two_comparable_passes():
    # One VV, one HH: two dated passes span the range, but no two share a
    # polarization, so there is no differenceable series to span.
    site = site_coverage("Mixed", [_pass("Mixed", 1, pols=["VV"]), _pass("Mixed", 5, pols=["HH"])])
    assert site.span_days == 4
    assert site.comparable_passes == 1
    assert site.comparable_span_days is None


def test_comparable_max_revisit_widens_when_a_cross_pol_pass_fills_a_gap():
    # VV imaged days 1 and 20; a lone HH pass at day 10 sits in between. The raw
    # dated cadence (1, 10, 20) reads a longest gap of 10 days, but that HH pass is
    # useless to a VV change run -- the VV series itself has a single 19-day gap,
    # which is the honest worst-case revisit of the analysable series.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 1, pols=["VV"]),
            _pass("Mixed", 10, pols=["HH"]),
            _pass("Mixed", 20, pols=["VV"]),
        ],
    )
    assert site.max_revisit_days == 10.0  # over the whole dated range
    assert site.comparable_passes == 2  # the two VV passes
    assert site.comparable_max_revisit_days == 19.0  # the VV series' own gap


def test_comparable_max_revisit_narrows_when_an_off_series_gap_inflates_the_raw():
    # VV imaged days 1, 2, 3 (tight); two HH passes at days 5 and 30 open a wide
    # hole. VV is the largest group, so the analysable series is 1, 2, 3 with a
    # worst gap of one day -- the raw 25-day gap lives entirely in the HH passes.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 1, pols=["VV"]),
            _pass("Mixed", 2, pols=["VV"]),
            _pass("Mixed", 3, pols=["VV"]),
            _pass("Mixed", 5, pols=["HH"]),
            _pass("Mixed", 30, pols=["HH"]),
        ],
    )
    assert site.comparable_passes == 3  # the three VV passes win the group
    assert site.max_revisit_days == 25.0  # the HH hole, over the whole dated range
    assert site.comparable_max_revisit_days == 1.0  # the tight VV cadence


def test_comparable_max_revisit_equals_max_under_one_polarization():
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (1, 7, 13)])
    assert site.comparable_max_revisit_days == site.max_revisit_days == 6.0


def test_comparable_max_revisit_null_with_fewer_than_two_comparable_passes():
    # One VV, one HH: two dated passes give a raw gap, but no two share a
    # polarization, so there is no differenceable series to measure a gap over.
    site = site_coverage("Mixed", [_pass("Mixed", 1, pols=["VV"]), _pass("Mixed", 5, pols=["HH"])])
    assert site.max_revisit_days == 4.0
    assert site.comparable_passes == 1
    assert site.comparable_min_revisit_days is None
    assert site.comparable_median_revisit_days is None
    assert site.comparable_max_revisit_days is None


def test_comparable_cadence_triple_is_measured_over_the_comparable_subset():
    # VV imaged days 1, 2, 6, 20 (gaps 1, 4, 14); two HH passes at days 8 and 9
    # (gap 1) ride along off-series. The raw dated cadence over 1,2,6,8,9,20 reads
    # a 1-day shortest gap and an 11-day longest, but the VV change series' own
    # gaps are 1, 4, 14 -- shortest 1, typical 4, worst 14 -- which is the cadence
    # a change run actually faces.
    site = site_coverage(
        "Mixed",
        [
            _pass("Mixed", 1, pols=["VV"]),
            _pass("Mixed", 2, pols=["VV"]),
            _pass("Mixed", 6, pols=["VV"]),
            _pass("Mixed", 20, pols=["VV"]),
            _pass("Mixed", 8, pols=["HH"]),
            _pass("Mixed", 9, pols=["HH"]),
        ],
    )
    assert site.comparable_passes == 4  # the four VV passes win the group
    # Raw cadence over every dated pass differs from the comparable one.
    assert site.max_revisit_days == 11.0  # 9 -> 20, over the whole dated range
    # The comparable (VV) series' own shortest / typical / worst gaps.
    assert site.comparable_min_revisit_days == 1.0
    assert site.comparable_median_revisit_days == 4.0
    assert site.comparable_max_revisit_days == 14.0


def test_comparable_cadence_triple_equals_the_raw_triple_under_one_polarization():
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (1, 4, 12)])
    # Gaps 3 and 8: shortest 3, typical 5.5, worst 8 -- and every pass is
    # comparable, so each twin equals its raw counterpart exactly.
    assert site.comparable_min_revisit_days == site.min_revisit_days == 3.0
    assert site.comparable_median_revisit_days == site.median_revisit_days == 5.5
    assert site.comparable_max_revisit_days == site.max_revisit_days == 8.0


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


def test_rank_by_comparable_prefers_the_deeper_differenceable_series():
    # "Broad" has more passes but they split across polarizations, so only two
    # (the VV pair) are differenceable; "Deep" has fewer passes, all VV, so all
    # three are. Raw ranking puts Broad first; comparable ranking flips them,
    # because the analysable series is what a change verb can actually use.
    pool = [
        *[_pass("Broad", d, pols=["VV"]) for d in (1, 2)],
        *[_pass("Broad", d, pols=["HH"]) for d in (3, 4)],
        _pass("Broad", 5, pols=["VH"]),
        *[_pass("Deep", d, pols=["VV"]) for d in (6, 7, 8)],
    ]
    by_passes = rank_site_coverage(pool, rank_by="passes")
    assert [(s.task, s.passes, s.comparable_passes) for s in by_passes] == [
        ("Broad", 5, 2),
        ("Deep", 3, 3),
    ]
    by_comparable = rank_site_coverage(pool, rank_by="comparable")
    assert [(s.task, s.passes, s.comparable_passes) for s in by_comparable] == [
        ("Deep", 3, 3),
        ("Broad", 5, 2),
    ]


def test_rank_by_comparable_promotes_a_deep_site_past_the_raw_top():
    # A deeply-analysable site with fewer raw passes than several broad ones must
    # still surface at top=1 under comparable ranking -- the key is applied before
    # the truncation, so it is not dropped by the raw-count cap first.
    pool = [
        *[_pass("MixA", d, pols=(["VV"] if d % 2 else ["HH"])) for d in (1, 2, 3, 4, 5, 6)],
        *[_pass("MixB", d, pols=(["VV"] if d % 2 else ["HH"])) for d in (7, 8, 9, 10, 11, 12)],
        *[_pass("Clean", d, pols=["VV"]) for d in (13, 14, 15, 16)],
    ]
    # Each Mix site has 6 raw passes (3 VV / 3 HH -> comparable 3); Clean has 4
    # raw passes, all VV -> comparable 4. Raw top=1 is a Mix; comparable top=1 is
    # Clean, even though two sites have more raw passes.
    assert rank_site_coverage(pool, top=1, rank_by="passes")[0].comparable_passes == 3
    assert rank_site_coverage(pool, top=1, rank_by="comparable")[0].task == "Clean"


def test_rank_by_agrees_when_every_pass_shares_one_polarization():
    pool = [
        *[_pass("Alpha", d, pols=["VV"]) for d in (1, 2, 3)],
        *[_pass("Bravo", d, pols=["VV"]) for d in (4, 5)],
    ]
    assert [s.task for s in rank_site_coverage(pool, rank_by="passes")] == ["Alpha", "Bravo"]
    assert [s.task for s in rank_site_coverage(pool, rank_by="comparable")] == ["Alpha", "Bravo"]


def test_rank_rejects_an_unknown_ranking():
    import pytest

    with pytest.raises(ValueError, match="rank_by must be one of"):
        rank_site_coverage([_pass("Alpha", 1)], rank_by="deepest")


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


def test_sites_cli_notes_usable_depth_only_when_it_undercuts_the_raw_count(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        # Mixed: three VV, one HH -- only three are differenceable together.
        *[_pass("Mixed", d, pols=["VV"]) for d in (1, 4, 8)],
        _pass("Mixed", 10, pols=["HH"]),
        # Uniform single-polarization site -- no 'usable' note needed.
        *[_pass("Clean", d, pols=["VV"]) for d in (2, 5)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites"])
    assert result.exit_code == 0, result.output
    assert "passes   : 4" in result.output and "3 usable" in result.output
    # The comparable (VV) subset spans days 1->8 (7d), narrower than the whole
    # 1->10 range the HH pass stretches, so the usable clause carries its span.
    assert "3 usable over 7d" in result.output
    # The uniform site's line carries no 'usable' clause.
    assert "passes   : 2 over" in result.output
    assert "2 usable" not in result.output


def test_sites_cli_revisit_line_notes_the_usable_series_own_longest_gap(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        # VV days 1 and 20 with a lone HH pass at day 10 between them: the raw
        # dated cadence reads a 10-day longest gap, but the VV change series has a
        # single 19-day gap the HH pass does nothing to close.
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 10, pols=["HH"]),
        _pass("Mixed", 20, pols=["VV"]),
        # Uniform single-polarization site -- the two cadences agree, no note.
        *[_pass("Clean", d, pols=["VV"]) for d in (2, 8)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites"])
    assert result.exit_code == 0, result.output
    assert "10d longest gap (19d across the usable series)" in result.output
    # The uniform site's revisit line carries no across-the-usable-series note.
    assert "6d longest gap\n" in result.output
    assert result.output.count("across the usable series") == 1


def test_sites_cli_rank_by_comparable_reorders_and_labels(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        # Broad: 5 raw passes, only the VV pair differenceable -> comparable 2.
        *[_pass("Broad", d, pols=["VV"]) for d in (1, 2)],
        *[_pass("Broad", d, pols=["HH"]) for d in (3, 4)],
        _pass("Broad", 5, pols=["VH"]),
        # Deep: 3 raw passes, all VV -> comparable 3.
        *[_pass("Deep", d, pols=["VV"]) for d in (6, 7, 8)],
    ]
    _patch_gather(monkeypatch, pool)

    raw = CliRunner().invoke(cli, ["sites"])
    assert raw.exit_code == 0, raw.output
    assert raw.output.index("Broad") < raw.output.index("Deep")
    assert "best-covered first." in raw.output

    comp = CliRunner().invoke(cli, ["sites", "--rank-by", "comparable"])
    assert comp.exit_code == 0, comp.output
    assert comp.output.index("Deep") < comp.output.index("Broad")
    assert "deepest usable series first." in comp.output


def test_sites_cli_rejects_unknown_rank_by(monkeypatch):
    from umbra_py.cli import cli

    _patch_gather(monkeypatch, [_pass("Alpha", d) for d in (1, 2)])
    result = CliRunner().invoke(cli, ["sites", "--rank-by", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output


def test_sites_cli_json_carries_pass_urls(monkeypatch):
    from umbra_py.cli import cli

    _patch_gather(monkeypatch, [_pass("Alpha", d) for d in (1, 5)])
    result = CliRunner().invoke(cli, ["sites", "--json"])
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in result.output.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["passes"] == 2
    assert rows[0]["comparable_passes"] == 2
    assert rows[0]["span_days"] == 4
    assert rows[0]["comparable_span_days"] == 4  # both passes comparable -> equal
    assert rows[0]["min_revisit_days"] == 4.0
    assert rows[0]["max_revisit_days"] == 4.0  # one gap: shortest == longest
    # Both passes comparable -> each cadence twin equals its raw counterpart.
    assert rows[0]["comparable_min_revisit_days"] == 4.0
    assert rows[0]["comparable_median_revisit_days"] == 4.0
    assert rows[0]["comparable_max_revisit_days"] == 4.0
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
