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


def test_comparable_polarizations_names_the_usable_series_signature():
    # The same mixed site: three VV, two HH. `polarizations` lists both, but the
    # usable series is the VV group -- so `comparable_polarizations` names just it,
    # the signature the comparable depth / span / cadence / hrefs are all over.
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
    assert site.polarizations == ("HH", "VV")
    assert site.comparable_polarizations == ("VV",)
    # It is the signature every pass in `comparable_hrefs` shares, so a `--pol VV`
    # filter over `hrefs` would reproduce exactly that selection.
    assert set(site.comparable_polarizations) < set(site.polarizations)


def test_comparable_polarizations_equals_polarizations_under_one_signature():
    # Every dated pass shares one signature, so naming the usable series' one adds
    # nothing over `polarizations` -- and the two are equal, exactly as the other
    # comparable twins equal their raw counterparts under one polarization.
    site = site_coverage("VV", [_pass("VV", d, pols=["VV"]) for d in (1, 2, 3)])
    assert site.comparable_polarizations == site.polarizations == ("VV",)


def test_comparable_polarizations_is_the_dual_pol_signature_when_shared():
    # A signature is the whole polarization tuple, not one channel: a dual-pol
    # series' comparable signature is both channels, which is what change verbs
    # difference together (they refuse a *mix* of signatures, not a dual-pol pass).
    site = site_coverage("Dual", [_pass("Dual", d, pols=["HH", "HV"]) for d in (1, 4)])
    assert site.comparable_polarizations == ("HH", "HV")
    assert site.comparable_passes == 2


def test_comparable_polarizations_empty_when_no_polarization_metadata():
    # Passes carrying no polarization group under the empty signature (as the frame
    # selector treats them), so the usable series exists but its signature is empty
    # -- an empty tuple, never None, exactly as `polarizations` is empty then.
    site = site_coverage("Bare", [_pass("Bare", d) for d in (1, 2, 3)])
    assert site.polarizations == ()
    assert site.comparable_passes == 3
    assert site.comparable_polarizations == ()


def test_comparable_polarizations_empty_when_nothing_dated():
    # No dated pass means no comparable group to name; the empty signature is told
    # apart from the no-metadata case above by `comparable_passes` being 0.
    undated = _pass("None", 1, pols=["VV"])
    undated.properties.pop("datetime")
    site = site_coverage("None", [undated])
    assert site.comparable_passes == 0
    assert site.comparable_polarizations == ()


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


def test_min_passes_counts_comparable_depth_under_comparable_ranking():
    # "Mixed" has three dated passes but each in a different polarization, so its
    # differenceable series is one pass deep (comparable_passes == 1). Under the raw
    # ranking min_passes=2 admits it (three raw passes); under comparable ranking it
    # must not, because the floor now measures the usable depth the ranking does.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 2, pols=["HH"]),
        _pass("Mixed", 3, pols=["VH"]),
        *[_pass("Deep", d, pols=["VV"]) for d in (4, 5)],
    ]
    by_passes = rank_site_coverage(pool, min_passes=2, rank_by="passes")
    assert [(s.task, s.comparable_passes) for s in by_passes] == [("Mixed", 1), ("Deep", 2)]
    # Comparable ranking with the same floor drops Mixed (usable depth 1 < 2) and
    # keeps only the genuinely differenceable series.
    by_comparable = rank_site_coverage(pool, min_passes=2, rank_by="comparable")
    assert [s.task for s in by_comparable] == ["Deep"]


def test_min_passes_floor_is_unchanged_under_the_default_passes_ranking():
    # A site whose raw count clears the floor but whose comparable series does not
    # still qualifies under the default ranking -- only comparable ranking narrows.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 2, pols=["HH"]),
    ]
    assert [s.task for s in rank_site_coverage(pool, min_passes=2)] == ["Mixed"]
    assert rank_site_coverage(pool, min_passes=2, rank_by="comparable") == []


# --------------------------------------------------------------------------- #
# active_since: the whole-site recency filter (live monitoring targets)
# --------------------------------------------------------------------------- #
def test_active_since_keeps_only_recently_imaged_sites():
    # Fresh's newest pass is 2024-01-22, Stale's is 2024-01-03; a cutoff between
    # them drops the stale series and keeps the actively-imaged one.
    pool = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],
        *[_pass("Stale", d) for d in (1, 2, 3)],
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["Fresh", "Stale"]
    assert [s.task for s in rank_site_coverage(pool, active_since="2024-01-10")] == ["Fresh"]


def test_active_since_is_boundary_inclusive_and_keeps_full_history():
    # A site whose newest pass falls exactly on the cutoff is kept (on or after),
    # and it keeps *all* its passes -- including the one before the cutoff --
    # because active_since selects whole sites, unlike start which truncates.
    pool = [_pass("Fresh", d) for d in (1, 5, 10)]
    [site] = rank_site_coverage(pool, active_since="2024-01-10")
    assert site.task == "Fresh"
    assert site.passes == 3  # the pre-cutoff passes on the 1st and 5th are retained
    assert site.first == "2024-01-01"
    # One day past the newest pass drops the site entirely.
    assert rank_site_coverage(pool, active_since="2024-01-11") == []


def test_active_since_is_orthogonal_to_rank_by():
    # The recency gate is on the site's newest pass, independent of which depth the
    # ranking measures: a broad-but-mixed fresh site and a deep stale one filter the
    # same way under either ranking (only their order differs).
    pool = [
        *[_pass("FreshMixed", d, pols=["VV" if d % 2 else "HH"]) for d in (20, 21, 22)],
        *[_pass("StaleDeep", d, pols=["VV"]) for d in (1, 2, 3)],
    ]
    for rank_by in ("passes", "comparable"):
        kept = rank_site_coverage(pool, active_since="2024-01-10", rank_by=rank_by)
        assert [s.task for s in kept] == ["FreshMixed"], rank_by


def test_active_since_none_applies_no_filter():
    pool = [_pass("Old", d) for d in (1, 2)]
    assert [s.task for s in rank_site_coverage(pool, active_since=None)] == ["Old"]


def test_active_since_accepts_a_relative_expression():
    # A relative bound resolves against today, so 2024 passes fall before any
    # plausible "6 months ago" cutoff and are dropped -- the same grammar
    # --start / --end accept, reused rather than a second parser.
    pool = [_pass("Old", d) for d in (1, 2)]
    assert rank_site_coverage(pool, active_since="6 months ago") == []


# --------------------------------------------------------------------------- #
# active_before: the upper recency bound (dormant series / activity windows)
# --------------------------------------------------------------------------- #
def test_active_before_keeps_only_sites_that_stopped_imaging():
    # The complement of active_since: a cutoff between the two series' newest passes
    # keeps the dormant (Stale) site and drops the still-active (Fresh) one.
    pool = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],
        *[_pass("Stale", d) for d in (1, 2, 3)],
    ]
    assert [s.task for s in rank_site_coverage(pool, active_before="2024-01-10")] == ["Stale"]


def test_active_before_is_boundary_inclusive_and_keeps_full_history():
    # A site whose newest pass falls exactly on the cutoff is kept (on or before),
    # with all its passes retained. One day before the newest pass drops it.
    pool = [_pass("Stale", d) for d in (1, 5, 10)]
    [site] = rank_site_coverage(pool, active_before="2024-01-10")
    assert site.task == "Stale"
    assert site.passes == 3
    assert rank_site_coverage(pool, active_before="2024-01-09") == []


def test_active_before_and_since_bound_the_newest_pass_to_a_window():
    # Set together they select sites whose *latest* pass falls within [since, before].
    pool = [
        *[_pass("TooOld", d) for d in (1, 2)],  # newest 2024-01-02, below the window
        *[_pass("InWindow", d) for d in (10, 12)],  # newest 2024-01-12, inside
        *[_pass("TooNew", d) for d in (25, 26)],  # newest 2024-01-26, above the window
    ]
    kept = rank_site_coverage(pool, active_since="2024-01-05", active_before="2024-01-20")
    assert [s.task for s in kept] == ["InWindow"]


def test_active_before_snaps_a_span_to_its_last_day():
    # A bare month snaps to its last day (2024-01-31, is_end), so a mid-month newest
    # pass is on or before it -- symmetric with the --end bound. Snapping to the
    # first day instead (as active_since does) would wrongly drop it.
    pool = [_pass("Jan", 15), _pass("Jan", 16)]  # newest 2024-01-16
    assert [s.task for s in rank_site_coverage(pool, active_before="2024-01")] == ["Jan"]


def test_active_before_none_applies_no_filter():
    pool = [_pass("Any", d) for d in (1, 2)]
    assert [s.task for s in rank_site_coverage(pool, active_before=None)] == ["Any"]


# --------------------------------------------------------------------------- #
# first_since / first_before: the onset (first-seen) filters -- the twins of the
# active_* pair, gating a site's *earliest* pass rather than its newest
# --------------------------------------------------------------------------- #
def test_first_since_keeps_only_newly_appeared_sites():
    # New's earliest pass is 2024-01-10, Old's is 2024-01-01; a cutoff between them
    # drops the long-established series and keeps the newly-appeared one.
    pool = [
        *[_pass("New", d) for d in (10, 11, 12)],
        *[_pass("Old", d) for d in (1, 2, 3)],
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["New", "Old"]
    assert [s.task for s in rank_site_coverage(pool, first_since="2024-01-05")] == ["New"]


def test_first_since_is_boundary_inclusive_and_keeps_full_history():
    # A site whose earliest pass falls exactly on the cutoff is kept (on or after),
    # and every pass is retained -- the onset gate selects whole sites, it does not
    # truncate. One day past the earliest pass drops the site entirely.
    pool = [_pass("New", d) for d in (10, 15, 20)]
    [site] = rank_site_coverage(pool, first_since="2024-01-10")
    assert site.task == "New"
    assert site.passes == 3
    assert site.last == "2024-01-20"  # later passes retained
    assert rank_site_coverage(pool, first_since="2024-01-11") == []


def test_first_before_keeps_only_long_established_sites():
    # The complement of first_since: a cutoff between the two series' earliest passes
    # keeps the long-established (Old) site and drops the newly-appeared (New) one.
    pool = [
        *[_pass("New", d) for d in (10, 11, 12)],
        *[_pass("Old", d) for d in (1, 2, 3)],
    ]
    assert [s.task for s in rank_site_coverage(pool, first_before="2024-01-05")] == ["Old"]


def test_first_before_snaps_a_span_to_its_last_day():
    # A bare month snaps to its last day (2024-01-31, is_end), so a mid-month earliest
    # pass is on or before it -- symmetric with active_before / --end. Snapping to the
    # first day instead would wrongly drop it.
    pool = [_pass("Jan", 15), _pass("Jan", 16)]  # earliest 2024-01-15
    assert [s.task for s in rank_site_coverage(pool, first_before="2024-01")] == ["Jan"]


def test_first_since_and_before_bound_the_onset_to_a_window():
    # Set together they select sites whose *earliest* pass falls within [since, before].
    pool = [
        *[_pass("StartedEarly", d) for d in (1, 20)],  # first 2024-01-01, below the window
        *[_pass("StartedMid", d) for d in (10, 22)],  # first 2024-01-10, inside
        *[_pass("StartedLate", d) for d in (25, 26)],  # first 2024-01-25, above the window
    ]
    kept = rank_site_coverage(pool, first_since="2024-01-05", first_before="2024-01-20")
    assert [s.task for s in kept] == ["StartedMid"]


def test_first_since_is_orthogonal_to_the_active_recency_filters():
    # Onset (when a site started) and recency (whether it is still going) are
    # independent axes. Both series start on 2024-01-01, so neither is "newly
    # appeared" -- first_since drops both regardless of their differing recency,
    # which active_since alone could not express.
    pool = [
        _pass("StillActive", 1),
        _pass("StillActive", 30),  # newest 2024-01-30
        _pass("WentDormant", 1),
        _pass("WentDormant", 3),  # newest 2024-01-03
    ]
    # active_since keeps only the still-active series (its newest pass is recent)...
    assert [s.task for s in rank_site_coverage(pool, active_since="2024-01-10")] == ["StillActive"]
    # ...but first_since gates the *earliest* pass, and both started on the 1st, so a
    # later onset cutoff drops both -- a selection the recency axis cannot make.
    assert rank_site_coverage(pool, first_since="2024-01-05") == []
    # And first_since=X with active_before=Y finds series that appeared after X and
    # had already gone dormant by Y -- the two axes composing.
    pool2 = [
        *[_pass("NewDormant", d) for d in (10, 12)],  # first 10th, newest 12th (dormant by 20th)
        *[_pass("NewActive", d) for d in (10, 28)],  # first 10th, newest 28th (still active)
    ]
    kept = rank_site_coverage(pool2, first_since="2024-01-05", active_before="2024-01-20")
    assert [s.task for s in kept] == ["NewDormant"]


def test_first_since_before_none_applies_no_filter():
    pool = [_pass("Any", d) for d in (1, 2)]
    kept = rank_site_coverage(pool, first_since=None, first_before=None)
    assert [s.task for s in kept] == ["Any"]


# --------------------------------------------------------------------------- #
# max_revisit: the worst-case cadence filter (reliably-imaged sites)
# --------------------------------------------------------------------------- #
def test_max_revisit_keeps_only_reliably_revisited_sites():
    # Steady's worst gap is 6 days; Gappy's is 15. A 10-day cadence bound keeps the
    # reliably-imaged site and drops the one with a long blind spot -- even though
    # both have the same pass count and the same median gap.
    pool = [
        *[_pass("Steady", d) for d in (1, 7, 13, 19)],  # gaps 6,6,6 -> worst 6
        *[_pass("Gappy", d) for d in (1, 7, 13, 28)],  # gaps 6,6,15 -> worst 15
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["Gappy", "Steady"]
    assert [s.task for s in rank_site_coverage(pool, max_revisit_days=10)] == ["Steady"]


def test_max_revisit_is_boundary_inclusive():
    # A site whose worst gap falls exactly on the bound is kept (at most, not below).
    pool = [_pass("Six", d) for d in (1, 7, 13)]  # worst gap 6
    assert [s.task for s in rank_site_coverage(pool, max_revisit_days=6)] == ["Six"]
    assert rank_site_coverage(pool, max_revisit_days=5.9) == []


def test_max_revisit_drops_a_site_with_no_measurable_cadence():
    # A single-pass site has no revisit gap, so it cannot be confirmed to meet any
    # cadence requirement and is dropped -- the same way active_since drops a site
    # with no datable pass (min_passes=1 admits it only when no cadence bound is set).
    pool = [_pass("Lonely", 1)]
    assert [s.task for s in rank_site_coverage(pool, min_passes=1)] == ["Lonely"]
    assert rank_site_coverage(pool, min_passes=1, max_revisit_days=30) == []


def test_max_revisit_gates_the_comparable_cadence_under_comparable_ranking():
    # A VV series on days 1 and 20 (a 19-day gap) with a lone HH pass at day 10
    # between them: the raw cadence reads a tight 10-day worst gap, but the series a
    # change verb can actually difference (VV only) has a 19-day hole the HH pass
    # does nothing to close. Under 'passes' the raw 10 clears a 12-day bound; under
    # 'comparable' the analysable 19 does not -- the cadence twin of min_passes
    # measuring comparable depth.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 10, pols=["HH"]),
        _pass("Mixed", 20, pols=["VV"]),
    ]
    assert [s.task for s in rank_site_coverage(pool, max_revisit_days=12)] == ["Mixed"]
    assert rank_site_coverage(pool, max_revisit_days=12, rank_by="comparable") == []
    # A bound wide enough for the 19-day VV gap keeps it under either ranking.
    assert [
        s.task for s in rank_site_coverage(pool, max_revisit_days=20, rank_by="comparable")
    ] == ["Mixed"]


def test_max_revisit_is_orthogonal_to_recency_and_promotes_past_the_top():
    # A cadence bound selects on the gap, not on depth or recency: a shallow but
    # tightly-imaged site survives a bound a deep-but-gappy one fails, and it is not
    # truncated away by a small --top before the filter runs (the whole-archive
    # correction the index path also makes).
    pool = [
        *[_pass("DeepGappy", d) for d in (1, 2, 3, 30)],  # 4 passes, worst gap 27
        *[_pass("Tight", d) for d in (10, 12)],  # 2 passes, worst gap 2
    ]
    # Without the cadence filter DeepGappy ranks first (more passes); with top=1 it
    # would be the only one returned. The cadence bound drops it and keeps Tight.
    assert [s.task for s in rank_site_coverage(pool, top=1)] == ["DeepGappy"]
    assert [s.task for s in rank_site_coverage(pool, top=1, max_revisit_days=5)] == ["Tight"]


def test_max_revisit_none_applies_no_filter_and_non_positive_is_rejected():
    import pytest

    pool = [_pass("Any", d) for d in (1, 30)]  # worst gap 29
    assert [s.task for s in rank_site_coverage(pool, max_revisit_days=None)] == ["Any"]
    for bad in (0, -1.0):
        with pytest.raises(ValueError, match="max_revisit_days must be positive"):
            rank_site_coverage(pool, max_revisit_days=bad)


# --------------------------------------------------------------------------- #
# median_revisit: the typical-cadence filter (usually-imaged-often sites)
# --------------------------------------------------------------------------- #
def test_median_revisit_keeps_sites_usually_imaged_often():
    # Steady's median gap is 2 days; Sparse's is 10. A 5-day typical-cadence bound
    # keeps the regularly-imaged site and drops the one usually left waiting -- even
    # though both have the same pass count.
    pool = [
        *[_pass("Steady", d) for d in (1, 3, 5, 25)],  # gaps 2,2,20 -> median 2
        *[_pass("Sparse", d) for d in (1, 11, 21, 24)],  # gaps 10,10,3 -> median 10
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["Sparse", "Steady"]
    assert [s.task for s in rank_site_coverage(pool, median_revisit_days=5)] == ["Steady"]


def test_median_revisit_is_the_complement_of_max_revisit():
    # The two cadence bounds select different sites. Bursty is usually imaged every
    # 2 days but has one 20-day outage; Even is imaged uniformly every 7 days.
    #   - median<=5 keeps Bursty (typical gap 2) and drops Even (typical gap 7)
    #   - max<=8    keeps Even (worst gap 7) and drops Bursty (worst gap 20)
    # so a site can pass one filter and fail the other in either direction -- which is
    # exactly why both exist. Set together they demand "usually tight AND never blind".
    pool = [
        *[_pass("Bursty", d) for d in (1, 3, 5, 25)],  # median 2, worst 20
        *[_pass("Even", d) for d in (1, 8, 15, 22)],  # median 7, worst 7
    ]
    assert [s.task for s in rank_site_coverage(pool, median_revisit_days=5)] == ["Bursty"]
    assert [s.task for s in rank_site_coverage(pool, max_revisit_days=8)] == ["Even"]
    # Both bounds at once: neither site is usually-tight *and* never-blind, so empty.
    assert rank_site_coverage(pool, median_revisit_days=5, max_revisit_days=8) == []


def test_median_revisit_is_boundary_inclusive():
    # A site whose median gap falls exactly on the bound is kept (at most, not below).
    pool = [_pass("Six", d) for d in (1, 7, 13)]  # gaps 6,6 -> median 6
    assert [s.task for s in rank_site_coverage(pool, median_revisit_days=6)] == ["Six"]
    assert rank_site_coverage(pool, median_revisit_days=5.9) == []


def test_median_revisit_drops_a_site_with_no_measurable_cadence():
    # A single-pass site has no revisit gap, so it cannot be confirmed to meet any
    # cadence requirement and is dropped -- exactly as the worst-case bound drops it.
    pool = [_pass("Lonely", 1)]
    assert [s.task for s in rank_site_coverage(pool, min_passes=1)] == ["Lonely"]
    assert rank_site_coverage(pool, min_passes=1, median_revisit_days=30) == []


def test_median_revisit_gates_the_comparable_cadence_under_comparable_ranking():
    # A VV series on days 1, 20 and 21 (gaps 19, 1 -> median 10) with a lone HH pass
    # at day 10: the raw dated series (gaps 9, 10, 1 -> median 9) reads tighter than
    # the VV series a change verb can actually difference (median 10). Under 'passes'
    # the raw 9 clears a 9.5-day bound; under 'comparable' the analysable 10 does not.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 10, pols=["HH"]),
        _pass("Mixed", 20, pols=["VV"]),
        _pass("Mixed", 21, pols=["VV"]),
    ]
    assert [s.task for s in rank_site_coverage(pool, median_revisit_days=9.5)] == ["Mixed"]
    assert rank_site_coverage(pool, median_revisit_days=9.5, rank_by="comparable") == []
    # A bound the 10-day VV median clears keeps it under either ranking.
    assert [
        s.task for s in rank_site_coverage(pool, median_revisit_days=10, rank_by="comparable")
    ] == ["Mixed"]


def test_median_revisit_is_orthogonal_to_recency_and_promotes_past_the_top():
    # A typical-cadence bound selects on the median gap, not on depth or recency: a
    # shallow but regularly-imaged site survives a bound a deep-but-sparse one fails,
    # and it is not truncated away by a small --top before the filter runs.
    pool = [
        *[_pass("DeepSparse", d) for d in (1, 12, 23, 28)],  # 4 passes, median 11
        *[_pass("Tight", d) for d in (10, 12)],  # 2 passes, median 2
    ]
    assert [s.task for s in rank_site_coverage(pool, top=1)] == ["DeepSparse"]
    assert [s.task for s in rank_site_coverage(pool, top=1, median_revisit_days=5)] == ["Tight"]


def test_median_revisit_none_applies_no_filter_and_non_positive_is_rejected():
    import pytest

    pool = [_pass("Any", d) for d in (1, 30)]  # median 29
    assert [s.task for s in rank_site_coverage(pool, median_revisit_days=None)] == ["Any"]
    for bad in (0, -1.0):
        with pytest.raises(ValueError, match="median_revisit_days must be positive"):
            rank_site_coverage(pool, median_revisit_days=bad)


# --------------------------------------------------------------------------- #
# min_span: the observation-baseline filter (long-baseline sites)
# --------------------------------------------------------------------------- #
def test_min_span_keeps_only_long_baseline_sites():
    # Long spans 29 days on 3 passes; Short spans 2 days on 3 passes. A 10-day
    # baseline bound keeps the long-baseline site and drops the short-window one --
    # even though Short has the *tighter* cadence, which is exactly the axis span is
    # not: cadence is the worst gap, span is the total observation window.
    pool = [
        *[_pass("Long", d) for d in (1, 15, 30)],  # span 29, worst gap 15
        *[_pass("Short", d) for d in (1, 2, 3)],  # span 2, worst gap 1
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["Long", "Short"]
    assert [s.task for s in rank_site_coverage(pool, min_span_days=10)] == ["Long"]


def test_min_span_is_boundary_inclusive():
    # A site whose span falls exactly on the bound is kept (at least, not above).
    pool = [_pass("Nine", d) for d in (1, 10)]  # span 9
    assert [s.task for s in rank_site_coverage(pool, min_span_days=9)] == ["Nine"]
    assert rank_site_coverage(pool, min_span_days=9.1) == []


def test_min_span_drops_a_site_with_no_measurable_span():
    # A single-pass site has no span, so it cannot be confirmed to meet any baseline
    # requirement and is dropped -- the same way max_revisit drops a site with no
    # measurable cadence (min_passes=1 admits it only when no span bound is set).
    pool = [_pass("Lonely", 1)]
    assert [s.task for s in rank_site_coverage(pool, min_passes=1)] == ["Lonely"]
    assert rank_site_coverage(pool, min_passes=1, min_span_days=5) == []


def test_min_span_gates_the_comparable_span_under_comparable_ranking():
    # A VV pair on days 1 and 10 (span 9) with a lone HH pass at day 30: the raw
    # dated series spans 29 days, but the series a change verb can actually difference
    # (VV only) spans just 9. Under 'passes' the raw 29 clears a 20-day bound; under
    # 'comparable' the analysable 9 does not -- the baseline twin of min_passes /
    # max_revisit measuring the comparable series.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 10, pols=["VV"]),
        _pass("Mixed", 30, pols=["HH"]),
    ]
    assert [s.task for s in rank_site_coverage(pool, min_span_days=20)] == ["Mixed"]
    assert rank_site_coverage(pool, min_span_days=20, rank_by="comparable") == []
    # A bound the 9-day VV span clears keeps it under either ranking.
    assert [s.task for s in rank_site_coverage(pool, min_span_days=5, rank_by="comparable")] == [
        "Mixed"
    ]


def test_min_span_is_orthogonal_to_cadence_and_promotes_past_the_top():
    # A baseline bound selects on the span, not on depth or cadence: a shallow but
    # long-baseline site survives a bound a deep-but-short-window one fails, and it is
    # not truncated away by a small --top before the filter runs (the whole-archive
    # correction the index path also makes). This is the complement of the max_revisit
    # promotion test -- there the tight-cadence site was promoted; here the long one.
    pool = [
        *[_pass("ShortDeep", d) for d in (10, 11, 12, 13)],  # 4 passes, span 3
        *[_pass("LongSparse", d) for d in (1, 30)],  # 2 passes, span 29
    ]
    assert [s.task for s in rank_site_coverage(pool, top=1)] == ["ShortDeep"]
    assert [s.task for s in rank_site_coverage(pool, top=1, min_span_days=20)] == ["LongSparse"]


def test_min_span_none_applies_no_filter_and_non_positive_is_rejected():
    import pytest

    pool = [_pass("Any", d) for d in (1, 2)]  # span 1
    assert [s.task for s in rank_site_coverage(pool, min_span_days=None)] == ["Any"]
    for bad in (0, -1.0):
        with pytest.raises(ValueError, match="min_span_days must be positive"):
            rank_site_coverage(pool, min_span_days=bad)


def test_min_span_and_max_revisit_are_independent_axes():
    # A site can be tightly-imaged over a short window, or sparsely over a long one.
    # min_span selects the second, max_revisit the first; combined they demand both.
    pool = [
        *[_pass("Burst", d) for d in (1, 2, 3, 4)],  # span 3, worst gap 1
        *[_pass("Slow", d) for d in (1, 16, 31)],  # span 30, worst gap 15
        *[_pass("Both", d) for d in (1, 8, 15, 22, 29)],  # span 28, worst gap 7
    ]
    # Long baseline alone: Slow and Both (Burst's window is too short).
    assert sorted(s.task for s in rank_site_coverage(pool, min_span_days=20)) == ["Both", "Slow"]
    # Tight cadence alone: Burst and Both (Slow has a 15-day hole).
    tight = rank_site_coverage(pool, max_revisit_days=10)
    assert sorted(s.task for s in tight) == ["Both", "Burst"]
    # Both axes at once: only Both is long *and* reliable.
    assert [s.task for s in rank_site_coverage(pool, min_span_days=20, max_revisit_days=10)] == [
        "Both"
    ]


# --------------------------------------------------------------------------- #
# max_span: the observation-baseline ceiling (short-lived sites)
# --------------------------------------------------------------------------- #
def test_max_span_keeps_only_short_baseline_sites():
    # The mirror of the min_span test: max_span selects the *complement* -- a
    # short-window burst is kept and a long-baseline series dropped. Long spans 29
    # days, Short spans 2; a 10-day ceiling keeps Short and drops Long.
    pool = [
        *[_pass("Long", d) for d in (1, 15, 30)],  # span 29
        *[_pass("Short", d) for d in (1, 2, 3)],  # span 2
    ]
    assert [s.task for s in rank_site_coverage(pool)] == ["Long", "Short"]
    assert [s.task for s in rank_site_coverage(pool, max_span_days=10)] == ["Short"]


def test_max_span_is_boundary_inclusive():
    # A site whose span falls exactly on the ceiling is kept (at most, not below).
    pool = [_pass("Nine", d) for d in (1, 10)]  # span 9
    assert [s.task for s in rank_site_coverage(pool, max_span_days=9)] == ["Nine"]
    assert rank_site_coverage(pool, max_span_days=8.9) == []


def test_max_span_and_min_span_bound_the_baseline_to_a_window():
    # Set together, the floor and the ceiling keep only sites whose span falls inside
    # the window -- exactly as active_since/active_before bound the newest pass. Mid
    # (span 10) is in [5, 20]; Short (span 2) is below it and Long (span 29) above.
    pool = [
        *[_pass("Short", d) for d in (1, 3)],  # span 2
        *[_pass("Mid", d) for d in (1, 11)],  # span 10
        *[_pass("Long", d) for d in (1, 30)],  # span 29
    ]
    assert [s.task for s in rank_site_coverage(pool, min_span_days=5, max_span_days=20)] == ["Mid"]
    # An inverted window (floor above ceiling) admits nothing, no error -- like an
    # inverted active_since/active_before window.
    assert rank_site_coverage(pool, min_span_days=20, max_span_days=5) == []


def test_max_span_drops_a_site_with_no_measurable_span():
    # A single-pass site has no confirmed baseline, so a ceiling drops it too -- the
    # same rule the floor applies, which is what lets the two compose as a window that
    # admits only a confirmed span (min_passes=1 admits it only with no span bound).
    pool = [_pass("Lonely", 1)]
    assert [s.task for s in rank_site_coverage(pool, min_passes=1)] == ["Lonely"]
    assert rank_site_coverage(pool, min_passes=1, max_span_days=5) == []


def test_max_span_gates_the_comparable_span_under_comparable_ranking():
    # A VV pair on days 1 and 10 (span 9) with a lone HH pass at day 30: the raw dated
    # series spans 29 days, but the differenceable (VV) series spans just 9. A 20-day
    # ceiling rejects the raw 29 under 'passes' yet keeps the analysable 9 under
    # 'comparable' -- off-polarization passes cannot inflate a baseline past a ceiling.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 10, pols=["VV"]),
        _pass("Mixed", 30, pols=["HH"]),
    ]
    assert rank_site_coverage(pool, max_span_days=20) == []
    assert [s.task for s in rank_site_coverage(pool, max_span_days=20, rank_by="comparable")] == [
        "Mixed"
    ]


def test_max_span_promotes_a_short_baseline_site_past_the_top():
    # A ceiling selects on span, not depth: a shallow short-window site survives a
    # ceiling a deep long-baseline one fails, and it is not truncated away by a small
    # --top before the filter runs -- the mirror of the min_span promotion test.
    pool = [
        *[_pass("LongDeep", d) for d in (1, 10, 20, 30)],  # 4 passes, span 29
        *[_pass("ShortSparse", d) for d in (1, 3)],  # 2 passes, span 2
    ]
    assert [s.task for s in rank_site_coverage(pool, top=1)] == ["LongDeep"]
    assert [s.task for s in rank_site_coverage(pool, top=1, max_span_days=5)] == ["ShortSparse"]


def test_max_span_none_applies_no_filter_and_non_positive_is_rejected():
    import pytest

    pool = [_pass("Any", d) for d in (1, 2)]  # span 1
    assert [s.task for s in rank_site_coverage(pool, max_span_days=None)] == ["Any"]
    for bad in (0, -1.0):
        with pytest.raises(ValueError, match="max_span_days must be positive"):
            rank_site_coverage(pool, max_span_days=bad)


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


def test_sites_cli_active_since_filters_to_recent_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],
        *[_pass("Stale", d) for d in (1, 2, 3)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--active-since", "2024-01-10"])
    assert result.exit_code == 0, result.output
    assert "Fresh" in result.output
    assert "Stale" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_active_since_empty_result_names_the_recency_bound(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Stale", d) for d in (1, 2, 3)]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--active-since", "2024-06-01"])
    assert result.exit_code == 0, result.output
    assert "imaged on/after 2024-06-01" in result.output
    assert "--active-since" in result.output  # the recency bound is offered to loosen


def test_sites_cli_active_before_filters_to_dormant_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],
        *[_pass("Stale", d) for d in (1, 2, 3)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--active-before", "2024-01-10"])
    assert result.exit_code == 0, result.output
    assert "Stale" in result.output
    assert "Fresh" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_active_window_empty_result_names_both_bounds(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Stale", d) for d in (1, 2, 3)]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(
        cli, ["sites", "--active-since", "2024-06-01", "--active-before", "2024-12-01"]
    )
    assert result.exit_code == 0, result.output
    assert "last imaged between 2024-06-01 and 2024-12-01" in result.output
    assert "--active-since" in result.output and "--active-before" in result.output


def test_sites_cli_first_since_filters_to_newly_appeared_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("New", d) for d in (10, 11, 12)],  # first 2024-01-10
        *[_pass("Old", d) for d in (1, 2, 3)],  # first 2024-01-01
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--first-since", "2024-01-05"])
    assert result.exit_code == 0, result.output
    assert "New" in result.output
    assert "Old" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_first_before_filters_to_established_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("New", d) for d in (10, 11, 12)],
        *[_pass("Old", d) for d in (1, 2, 3)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--first-before", "2024-01-05"])
    assert result.exit_code == 0, result.output
    assert "Old" in result.output
    assert "New" not in result.output


def test_sites_cli_first_window_empty_result_names_both_bounds(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Old", d) for d in (1, 2, 3)]  # first 2024-01-01, outside the window
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(
        cli, ["sites", "--first-since", "2024-06-01", "--first-before", "2024-12-01"]
    )
    assert result.exit_code == 0, result.output
    assert "first imaged between 2024-06-01 and 2024-12-01" in result.output
    assert "--first-since" in result.output and "--first-before" in result.output


def test_sites_cli_max_revisit_filters_to_reliably_imaged_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Steady", d) for d in (1, 7, 13)],  # worst gap 6
        *[_pass("Gappy", d) for d in (1, 7, 28)],  # worst gap 21
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--max-revisit", "10"])
    assert result.exit_code == 0, result.output
    assert "Steady" in result.output
    assert "Gappy" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_max_revisit_empty_result_names_the_cadence_bound(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Gappy", d) for d in (1, 7, 28)]  # worst gap 21
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--max-revisit", "10"])
    assert result.exit_code == 0, result.output
    assert "revisited within 10d" in result.output
    assert "--max-revisit" in result.output  # the cadence bound is offered to loosen


def test_sites_cli_median_revisit_filters_to_usually_imaged_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Steady", d) for d in (1, 3, 5, 25)],  # median gap 2
        *[_pass("Sparse", d) for d in (1, 11, 21, 24)],  # median gap 10
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--median-revisit", "5"])
    assert result.exit_code == 0, result.output
    assert "Steady" in result.output
    assert "Sparse" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_median_revisit_empty_result_names_the_cadence_bound(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Sparse", d) for d in (1, 11, 21, 24)]  # median gap 10
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--median-revisit", "5"])
    assert result.exit_code == 0, result.output
    assert "typically within 5d" in result.output
    assert "--median-revisit" in result.output  # offered to loosen


def test_sites_cli_min_span_filters_to_long_baseline_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Long", d) for d in (1, 15, 30)],  # span 29
        *[_pass("Short", d) for d in (1, 2, 3)],  # span 2
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--min-span", "10"])
    assert result.exit_code == 0, result.output
    assert "Long" in result.output
    assert "Short" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_min_span_empty_result_names_the_baseline_bound(monkeypatch):
    from umbra_py.cli import cli

    pool = [_pass("Short", d) for d in (1, 2, 3)]  # span 2
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--min-span", "10"])
    assert result.exit_code == 0, result.output
    assert "imaged over 10d+" in result.output
    assert "--min-span" in result.output  # the baseline bound is offered to loosen


def test_sites_cli_max_span_filters_to_short_baseline_sites(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        *[_pass("Long", d) for d in (1, 15, 30)],  # span 29
        *[_pass("Short", d) for d in (1, 2, 3)],  # span 2
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--max-span", "10"])
    assert result.exit_code == 0, result.output
    assert "Short" in result.output
    assert "Long" not in result.output
    assert "1 site(s), best-covered first." in result.output


def test_sites_cli_span_window_names_both_bounds_when_empty(monkeypatch):
    from umbra_py.cli import cli

    # A window that admits nothing names the range it asked for and offers both bounds.
    pool = [_pass("Long", d) for d in (1, 15, 30)]  # span 29
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites", "--min-span", "5", "--max-span", "10"])
    assert result.exit_code == 0, result.output
    assert "imaged over 5-10d" in result.output
    assert "--min-span" in result.output
    assert "--max-span" in result.output


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


def test_sites_cli_pol_line_names_the_usable_series_when_the_site_is_mixed(monkeypatch):
    from umbra_py.cli import cli

    pool = [
        # Mixed: three VV, one HH -- the pol line lists both but names VV as usable.
        *[_pass("Mixed", d, pols=["VV"]) for d in (1, 4, 8)],
        _pass("Mixed", 10, pols=["HH"]),
        # Uniform single-polarization site -- one signature, so no usable clause.
        *[_pass("Clean", d, pols=["VV"]) for d in (2, 5)],
    ]
    _patch_gather(monkeypatch, pool)
    result = CliRunner().invoke(cli, ["sites"])
    assert result.exit_code == 0, result.output
    assert "pol      : HH, VV (usable: VV)" in result.output
    # The uniform site's pol line carries no usable clause.
    assert "pol      : VV\n" in result.output
    assert result.output.count("(usable:") == 1


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


def test_sites_cli_min_passes_gates_comparable_depth(monkeypatch):
    from umbra_py.cli import cli

    # Mixed has 3 raw passes but a comparable depth of 1 (each a different pol);
    # Deep has 2 VV passes -> comparable depth 2.
    pool = [
        _pass("Mixed", 1, pols=["VV"]),
        _pass("Mixed", 2, pols=["HH"]),
        _pass("Mixed", 3, pols=["VH"]),
        *[_pass("Deep", d, pols=["VV"]) for d in (4, 5)],
    ]
    _patch_gather(monkeypatch, pool)

    # Default 'passes' ranking with --min-passes 2 keeps both sites.
    raw = CliRunner().invoke(cli, ["sites", "--min-passes", "2"])
    assert raw.exit_code == 0, raw.output
    assert "Mixed" in raw.output and "Deep" in raw.output

    # Comparable ranking floors the usable depth, so Mixed (usable 1) drops out.
    comp = CliRunner().invoke(cli, ["sites", "--min-passes", "2", "--rank-by", "comparable"])
    assert comp.exit_code == 0, comp.output
    assert "Deep" in comp.output and "Mixed" not in comp.output

    # And a floor nothing clears reports the comparable depth it measured.
    none = CliRunner().invoke(cli, ["sites", "--min-passes", "3", "--rank-by", "comparable"])
    assert none.exit_code == 0, none.output
    assert "3+ comparable passes" in none.output


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
    # Both passes carry no polarization metadata, so the usable series' signature
    # is the empty one -- an empty list in JSON, matching `polarizations`.
    assert rows[0]["polarizations"] == []
    assert rows[0]["comparable_polarizations"] == []
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
