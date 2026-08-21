"""Tests for the ``umbra showcase`` static-site composer.

The showcase is a *composer*: it reuses the demo explorer and the PMTiles viewer
the toolkit already produces and ties them together with a landing page. So the
contract to pin down is (1) the landing page carries the right links, stats and
attribution and drops the cards it has no target for, and (2) the assembler
writes exactly the files the inputs justify and copies the basemap in beside its
viewer. It is stdlib-only, so none of this needs a network or the viz extra.

The featured gallery adds a third contract: which repeat-imaged sites get
precomputed (a pure, deterministic selection) and that one unrenderable site is
skipped rather than failing the whole build. The one step that would need the
viz extra -- rendering a composite -- goes through an injectable renderer, so
these stay offline too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from umbra_py import pmtiles, showcase
from umbra_py.cli import cli
from umbra_py.constants import DOCS_URL
from umbra_py.models import UmbraItem

REPO_ROOT = Path(__file__).resolve().parents[1]


def _item(item_id: str = "a", lon: float = -110.0, lat: float = 39.0) -> UmbraItem:
    """A minimal footprinted item (mirrors test_pmtiles/_item)."""
    d = 0.02
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "bbox": [lon - d, lat - d, lon + d, lat + d],
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon - d, lat - d],
                    [lon + d, lat - d],
                    [lon + d, lat + d],
                    [lon - d, lat + d],
                    [lon - d, lat - d],
                ]
            ],
        },
        "properties": {"datetime": "2024-01-01T00:00:00Z"},
        "assets": {},
    }
    return UmbraItem.from_dict(
        doc, href=f"https://x.s3.amazonaws.com/tasks/Site/t/{item_id}/i.json"
    )


def _pass(task: str, day: int, *, place: str | None = None) -> UmbraItem:
    """One acquisition of ``task`` on 2024-01-``day`` (the featured-gallery unit:
    what matters is the task grouping and the date, not the footprint)."""
    item = UmbraItem.from_dict(
        {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": f"{task}-{day}",
            "bbox": [-110.0, 39.0, -109.9, 39.1],
            "geometry": None,
            "properties": {"datetime": f"2024-01-{day:02d}T00:00:00Z"},
            "assets": {},
        },
        href=f"https://x.s3.amazonaws.com/sar-data/tasks/{task}/t/{day}/i.json",
    )
    item.place = place
    return item


# --- landing page ---------------------------------------------------------
def test_docs_url_matches_cname_and_mkdocs():
    """The custom domain is one fact; CNAME, mkdocs, and the showcase must agree."""
    assert showcase.DEFAULT_DOCS_URL == DOCS_URL
    assert DOCS_URL == "https://umbra-py.space/"
    cname = (REPO_ROOT / "docs_src" / "CNAME").read_text(encoding="utf-8").strip()
    assert DOCS_URL.rstrip("/") == f"https://{cname}"
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert f"site_url: {DOCS_URL}" in mkdocs


def test_build_showcase_full_page():
    html = showcase.build_showcase(
        map_href="map.html",
        explore_href="explore.html",
        item_count=1234,
        updated="2026-07-20",
    )
    assert html.startswith("<!DOCTYPE html>")
    # Both artifact cards present when both targets exist.
    assert 'href="map.html"' in html
    assert 'href="explore.html"' in html
    # Stats line renders the count (thousands-separated) and the freshness stamp.
    assert "1,234 acquisitions" in html
    assert "updated 2026-07-20" in html
    # Mandatory license attribution + the honesty disclaimer.
    assert "CC BY 4.0" in html
    assert "Not affiliated" in html
    # Project links default to this repo / the published docs site.
    assert "github.com/reesehammer/umbra-py" in html
    assert "umbra-py.space" in html


def test_build_showcase_drops_absent_cards():
    """A build with no basemap and no explorer still yields a coherent page —
    just the docs/source cards, no dangling links or stats separator."""
    html = showcase.build_showcase()
    assert 'href="map.html"' not in html
    assert 'href="explore.html"' not in html
    assert "Read the docs" in html and "Get the source" in html
    # No stats line when neither count nor date is known.
    assert 'class="stats"' not in html


def test_build_showcase_singular_and_custom_links():
    html = showcase.build_showcase(
        item_count=1,
        repo_url="https://example.com/fork",
        docs_url="https://example.com/docs/",
        title="My SAR site",
    )
    assert "1 acquisition" in html and "1 acquisitions" not in html
    assert "https://example.com/fork" in html
    assert "https://example.com/docs/" in html
    assert "<title>My SAR site</title>" in html


# --- assembler ------------------------------------------------------------
def test_assemble_writes_all_three_and_copies_pmtiles(tmp_path):
    items = [_item("a"), _item("b", -111.0, 40.0)]
    pm = tmp_path / "catalog.pmtiles"
    pmtiles.write_pmtiles(items, pm)

    out = tmp_path / "site"
    index = showcase.assemble_showcase(out, items=items, pmtiles_path=pm, updated="2026-07-20")

    assert index == out / "index.html"
    for name in ("index.html", "map.html", "explore.html", "catalog.pmtiles"):
        assert (out / name).exists(), name
    # The basemap is copied in, and the viewer references it by name (relocatable).
    assert (out / "catalog.pmtiles").read_bytes()[:7] == b"PMTiles"
    assert "catalog.pmtiles" in (out / "map.html").read_text()
    # The landing page links both artifacts and reports the item count.
    idx = index.read_text()
    assert 'href="map.html"' in idx and 'href="explore.html"' in idx
    assert "2 acquisitions" in idx


def test_assemble_map_only(tmp_path):
    pm = tmp_path / "catalog.pmtiles"
    pmtiles.write_pmtiles([_item()], pm)
    out = tmp_path / "site"
    showcase.assemble_showcase(out, pmtiles_path=pm)

    assert (out / "map.html").exists() and (out / "catalog.pmtiles").exists()
    assert not (out / "explore.html").exists()
    idx = (out / "index.html").read_text()
    assert 'href="map.html"' in idx and 'href="explore.html"' not in idx


def test_assemble_explore_only(tmp_path):
    out = tmp_path / "site"
    showcase.assemble_showcase(out, items=[_item()])

    assert (out / "explore.html").exists()
    assert not (out / "map.html").exists()
    assert not list(out.glob("*.pmtiles"))
    idx = (out / "index.html").read_text()
    assert 'href="explore.html"' in idx and 'href="map.html"' not in idx


def test_assemble_tolerates_pmtiles_already_in_dest(tmp_path):
    """A basemap handed to us that is already inside dest_dir is used in place,
    not copied onto itself (which would raise SameFileError)."""
    out = tmp_path / "site"
    out.mkdir()
    pm = out / "catalog.pmtiles"
    pmtiles.write_pmtiles([_item()], pm)

    showcase.assemble_showcase(out, pmtiles_path=pm)
    assert (out / "map.html").exists()
    assert pm.read_bytes()[:7] == b"PMTiles"


def test_assemble_forwards_demo_kwargs(tmp_path):
    out = tmp_path / "site"
    showcase.assemble_showcase(out, items=[_item()], demo_kwargs={"subtitle": "Utah beet pilers"})
    assert "Utah beet pilers" in (out / "explore.html").read_text()


# --- CLI ------------------------------------------------------------------
def test_cli_showcase_builds_site(tmp_path, monkeypatch):
    items = [_item("a"), _item("b", -111.0, 40.0)]
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: items)
    pm = tmp_path / "catalog.pmtiles"
    pmtiles.write_pmtiles(items, pm)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--pmtiles",
            str(pm),
            "--out",
            str(out),
            "--no-lazy-imagery",
            "--updated",
            "2026-07-20",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.html").exists()
    assert (out / "map.html").exists()
    assert (out / "explore.html").exists()
    assert "Wrote showcase site" in result.output


def test_cli_showcase_no_explore_map_only(tmp_path, monkeypatch):
    # --no-explore must not gather items at all.
    def _boom(**kwargs):  # pragma: no cover - asserted not called
        raise AssertionError("should not gather items with --no-explore")

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", _boom)
    pm = tmp_path / "catalog.pmtiles"
    pmtiles.write_pmtiles([_item()], pm)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--pmtiles", str(pm), "--out", str(out), "--no-explore"]
    )
    assert result.exit_code == 0, result.output
    assert (out / "map.html").exists()
    assert not (out / "explore.html").exists()


def test_cli_showcase_rejects_both_basemap_sources(tmp_path):
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--pmtiles",
            str(tmp_path / "x.pmtiles"),
            "--fetch-pmtiles",
            "--out",
            str(tmp_path / "site"),
            "--no-explore",
        ],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_cli_showcase_url_requires_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: [_item()])
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--pmtiles-url",
            "https://x/y.pmtiles",
            "--out",
            str(tmp_path / "s"),
        ],
    )
    assert result.exit_code != 0
    assert "--pmtiles-url only applies with --fetch-pmtiles" in result.output


def test_cli_showcase_nothing_to_show(tmp_path):
    result = CliRunner().invoke(cli, ["showcase", "--no-explore", "--out", str(tmp_path / "site")])
    assert result.exit_code != 0
    assert "Nothing to show" in result.output


def test_cli_showcase_fetch_pmtiles(tmp_path, monkeypatch):
    """--fetch-pmtiles pulls the published basemap into the output dir."""
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: [_item()])
    archive = pmtiles.build_pmtiles([_item("a"), _item("b", -111.0, 40.0)], max_zoom=3)

    def fake_fetch(dest, *, url=None, progress=None):
        Path(dest).write_bytes(archive)
        return Path(dest)

    monkeypatch.setattr("umbra_py.pmtiles.fetch_prebuilt_pmtiles", fake_fetch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--local", "--fetch-pmtiles", "--out", str(out), "--no-lazy-imagery"]
    )
    assert result.exit_code == 0, result.output
    assert (out / "catalog.pmtiles").read_bytes()[:7] == b"PMTiles"
    assert (out / "map.html").exists()


# --- featured-site selection ----------------------------------------------
def test_select_featured_sites_ranks_by_pass_count():
    """The most repeat-imaged sites win, ties broken by task name, so the
    selection is reproducible for a given pool."""
    items = [
        *[_pass("Bravo", d) for d in (1, 2, 3)],
        *[_pass("Alpha", d) for d in (4, 5, 6)],
        *[_pass("Delta", d) for d in (7, 8)],
        _pass("Echo", 9),  # single pass -- can't be composited
    ]
    sites = showcase.select_featured_sites(items, count=6)

    assert [s.task for s in sites] == ["Alpha", "Bravo", "Delta"]
    # Passes come back oldest-first, ready for a change composite.
    assert [i.datetime.day for i in sites[0].items] == [4, 5, 6]


def test_select_featured_sites_active_since_filters_by_recency():
    """The recency filter drops a stale site by its newest pass and keeps each
    survivor's full history (whole-site selection, not per-pass truncation)."""
    items = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],  # newest 2024-01-22
        *[_pass("Stale", d) for d in (1, 2, 3)],  # newest 2024-01-03
    ]
    kept = showcase.select_featured_sites(items, active_since="2024-01-10")
    assert [s.task for s in kept] == ["Fresh"]
    # A mid-series cutoff on Fresh's own range keeps all three passes (inclusive).
    survive = showcase.select_featured_sites(items, active_since="2024-01-20")
    assert [s.task for s in survive] == ["Fresh"]
    assert [i.datetime.day for i in survive[0].items] == [20, 21, 22]


def test_select_featured_sites_active_before_selects_dormant_sites_and_windows():
    """active_before keeps a site by its newest pass being on or before the cutoff
    (the complement of active_since), and with it bounds the newest pass to a
    window, retaining each survivor's full history."""
    items = [
        *[_pass("Fresh", d) for d in (20, 21, 22)],  # newest 2024-01-22
        *[_pass("Stale", d) for d in (1, 2, 3)],  # newest 2024-01-03
    ]
    dormant = showcase.select_featured_sites(items, active_before="2024-01-10")
    assert [s.task for s in dormant] == ["Stale"]
    assert [i.datetime.day for i in dormant[0].items] == [1, 2, 3]  # full history kept
    # With active_since it selects sites whose newest pass falls within the window.
    window = showcase.select_featured_sites(
        items, active_since="2024-01-15", active_before="2024-01-25"
    )
    assert [s.task for s in window] == ["Fresh"]


def test_select_featured_sites_first_since_before_filter_by_onset():
    """first_since / first_before gate a site's *earliest* pass (the onset twins of
    the active_* recency pair), selecting newly-appeared and long-established series
    and, together, an onset window -- retaining each survivor's full history."""
    items = [
        *[_pass("New", d) for d in (10, 11, 12)],  # first 2024-01-10
        *[_pass("Old", d) for d in (1, 2, 3)],  # first 2024-01-01
    ]
    newly = showcase.select_featured_sites(items, first_since="2024-01-05")
    assert [s.task for s in newly] == ["New"]
    assert [i.datetime.day for i in newly[0].items] == [10, 11, 12]  # full history kept
    established = showcase.select_featured_sites(items, first_before="2024-01-05")
    assert [s.task for s in established] == ["Old"]
    # Set together they bound the onset to a window (nothing has an onset in [4, 6]).
    windowed = showcase.select_featured_sites(
        items, first_since="2024-01-04", first_before="2024-01-06"
    )
    assert windowed == []


def test_select_featured_sites_max_revisit_keeps_reliably_imaged_sites():
    """max_revisit keeps a site only if its worst-case revisit gap is at most the
    bound, dropping a series with a long blind spot and retaining full history."""
    items = [
        *[_pass("Steady", d) for d in (1, 7, 13)],  # worst gap 6
        *[_pass("Gappy", d) for d in (1, 7, 28)],  # worst gap 21
    ]
    kept = showcase.select_featured_sites(items, max_revisit_days=10)
    assert [s.task for s in kept] == ["Steady"]
    assert [i.datetime.day for i in kept[0].items] == [1, 7, 13]  # full history kept


def test_select_featured_sites_median_revisit_keeps_usually_imaged_sites():
    """median_revisit keeps a site only if its *typical* (median) revisit gap is at
    most the bound -- the complement of max_revisit: a mostly-tight series with one
    long outage passes here but fails the worst-case bound, and vice versa."""
    items = [
        *[_pass("Bursty", d) for d in (1, 3, 5, 25)],  # median 2, worst 20
        *[_pass("Even", d) for d in (1, 8, 15, 22)],  # median 7, worst 7
    ]
    kept = showcase.select_featured_sites(items, median_revisit_days=5)
    assert [s.task for s in kept] == ["Bursty"]
    assert [i.datetime.day for i in kept[0].items] == [1, 3, 5, 25]  # full history kept
    # The worst-case bound selects the other site, confirming the two are complements.
    kept_max = showcase.select_featured_sites(items, max_revisit_days=8)
    assert [s.task for s in kept_max] == ["Even"]


def test_select_featured_sites_min_span_keeps_long_baseline_sites():
    """min_span keeps a site only if its observation span is at least the bound,
    dropping a short-window series and retaining full history -- a different axis from
    max_revisit (baseline, not cadence: Short has the tighter cadence but is dropped)."""
    items = [
        *[_pass("Long", d) for d in (1, 15, 30)],  # span 29, worst gap 15
        *[_pass("Short", d) for d in (1, 2, 3)],  # span 2, worst gap 1
    ]
    kept = showcase.select_featured_sites(items, min_span_days=10)
    assert [s.task for s in kept] == ["Long"]
    assert [i.datetime.day for i in kept[0].items] == [1, 15, 30]  # full history kept


def _pass_pol(task: str, day: int, pol: str) -> UmbraItem:
    """A featured-gallery pass carrying a polarization, so the comparable ranking
    (largest same-polarization dated subset) can be exercised."""
    item = UmbraItem.from_dict(
        {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": f"{task}-{day}-{pol}",
            "bbox": [-110.0, 39.0, -109.9, 39.1],
            "geometry": None,
            "properties": {
                "datetime": f"2024-01-{day:02d}T00:00:00Z",
                "sar:polarizations": [pol],
            },
            "assets": {},
        },
        href=f"https://x.s3.amazonaws.com/sar-data/tasks/{task}/t/{day}/i.json",
    )
    return item


def test_select_featured_sites_rank_by_comparable_uses_analysable_depth():
    """rank_by='comparable' orders by the largest same-polarization dated subset,
    so a broad-but-mixed site cannot outrank a deeper single-polarization one; the
    default 'passes' ranking (what the gallery uses) is unchanged."""
    items = [
        # Broad: 5 passes, only the VV pair differenceable together.
        *[_pass_pol("Broad", d, "VV") for d in (1, 2)],
        *[_pass_pol("Broad", d, "HH") for d in (3, 4)],
        _pass_pol("Broad", 5, "VH"),
        # Deep: 3 passes, all VV.
        *[_pass_pol("Deep", d, "VV") for d in (6, 7, 8)],
    ]
    assert [s.task for s in showcase.select_featured_sites(items)] == ["Broad", "Deep"]
    ranked = showcase.select_featured_sites(items, rank_by="comparable")
    assert [s.task for s in ranked] == ["Deep", "Broad"]


def test_select_featured_sites_rank_by_recency_and_span_order_the_temporal_axes():
    """rank_by='recency' orders by each site's newest pass and rank_by='span' by its
    observation baseline -- the axes the moat already filters on -- while 'passes'
    (the gallery default) still orders by raw pass count."""
    items = [
        *[_pass("Deep", d) for d in (1, 2, 3, 4, 5)],  # passes 5, newest 5, span 4
        *[_pass("Recent", d) for d in (20, 21)],  # passes 2, newest 21, span 1
        *[_pass("Wide", d) for d in (1, 28)],  # passes 2, newest 28, span 27
    ]
    assert [s.task for s in showcase.select_featured_sites(items)] == ["Deep", "Recent", "Wide"]
    assert [s.task for s in showcase.select_featured_sites(items, rank_by="recency")] == [
        "Wide",
        "Recent",
        "Deep",
    ]
    assert [s.task for s in showcase.select_featured_sites(items, rank_by="span")] == [
        "Wide",
        "Deep",
        "Recent",
    ]


def test_select_featured_sites_recency_promotes_a_recent_site_past_the_count_cap():
    """A shallow but recent site surfaces at count=1 under recency ranking even
    though a deeper site has more raw passes -- the key is applied before the
    truncation, matching the comparable ranking's promotion."""
    items = [
        *[_pass("Deep", d) for d in (1, 2, 3, 4, 5)],  # deepest, but oldest
        *[_pass("Recent", d) for d in (20, 21)],  # newest
    ]
    assert [s.task for s in showcase.select_featured_sites(items, count=1)] == ["Deep"]
    recent = showcase.select_featured_sites(items, count=1, rank_by="recency")
    assert [s.task for s in recent] == ["Recent"]
    assert [i.datetime.day for i in recent[0].items] == [20, 21]  # full history kept


def test_select_featured_sites_min_passes_gates_comparable_depth_under_comparable():
    """Under rank_by='comparable', min_passes floors the usable (comparable) depth,
    not the raw count -- a site whose raw passes clear the floor but whose
    differenceable series does not is dropped rather than ranked last."""
    items = [
        # Mixed: 3 dated passes, each a different polarization -> comparable depth 1.
        _pass_pol("Mixed", 1, "VV"),
        _pass_pol("Mixed", 2, "HH"),
        _pass_pol("Mixed", 3, "VH"),
        # Deep: 2 passes, both VV -> comparable depth 2.
        *[_pass_pol("Deep", d, "VV") for d in (4, 5)],
    ]
    # Default 'passes' floor admits Mixed (3 raw passes >= 2); comparable floor
    # drops it (usable depth 1 < 2) and keeps only the differenceable series.
    by_passes = showcase.select_featured_sites(items, min_passes=2)
    assert {s.task for s in by_passes} == {"Mixed", "Deep"}
    by_comparable = showcase.select_featured_sites(items, min_passes=2, rank_by="comparable")
    assert [s.task for s in by_comparable] == ["Deep"]


def test_select_featured_sites_rejects_unknown_rank_by():
    import pytest

    with pytest.raises(ValueError, match="rank_by must be one of"):
        showcase.select_featured_sites([_pass("Alpha", 1)], rank_by="deepest")


def test_select_featured_sites_respects_count_and_min_passes():
    items = [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]

    assert [s.task for s in showcase.select_featured_sites(items, count=1)] == ["Alpha"]
    # Three frames need three passes: Bravo's two no longer qualify.
    assert [s.task for s in showcase.select_featured_sites(items, min_passes=3)] == ["Alpha"]
    assert showcase.select_featured_sites(items, count=0) == []


def test_select_featured_sites_drops_ungroupable_items():
    """No task (nothing to group by) or no datetime (nothing to order by) means
    the item can't feed a change composite."""
    undated = UmbraItem.from_dict(
        {"type": "Feature", "stac_version": "1.0.0", "id": "u", "properties": {}, "assets": {}},
        href="https://x.s3.amazonaws.com/sar-data/tasks/Alpha/t/9/i.json",
    )
    untasked = UmbraItem.from_dict(
        {
            "type": "Feature",
            "stac_version": "1.0.0",
            "id": "n",
            "properties": {"datetime": "2024-01-01T00:00:00Z"},
            "assets": {},
        },
        href=None,
    )
    pool = [_pass("Alpha", 1), undated, untasked, untasked]

    assert showcase.select_featured_sites(pool) == []


def test_featured_site_label_slug_and_dates():
    site = showcase.select_featured_sites(
        [_pass("Beet Piler, ND", 5, place="Grand Forks"), _pass("Beet Piler, ND", 9)]
    )[0]

    # A baked place label beats the task codename; the slug is URL-safe.
    assert site.label == "Grand Forks"
    assert site.slug == "beet-piler-nd"
    assert site.date_range == "2024-01-05 \N{EN DASH} 2024-01-09"
    # Falls back to the task when no pass carries a place label, and passes that
    # share a day report the single date rather than an empty range.
    same_day = showcase.select_featured_sites([_pass("Alpha", 1), _pass("Alpha", 1)])[0]
    assert same_day.label == "Alpha"
    assert same_day.date_range == "2024-01-01"
    # A directly-built site with nothing dated has no range to report.
    assert showcase.FeaturedSite(task="Alpha", items=[]).date_range is None


# --- featured gallery -----------------------------------------------------
def test_build_showcase_has_no_featured_section_by_default():
    html = showcase.build_showcase(explore_href="explore.html")
    assert "What SAR change looks like" not in html
    assert 'class="featured"' not in html


def test_assemble_renders_featured_with_injected_renderer(tmp_path):
    sites = showcase.select_featured_sites(
        [
            *[_pass("Alpha", d, place="Centerfield, Utah") for d in (1, 5, 9)],
            *[_pass("Bravo", d) for d in (2, 4)],
        ]
    )
    rendered: list[tuple[int, Path]] = []

    def fake_render(items, dest):
        rendered.append((len(items), dest))
        dest.write_bytes(b"\x89PNG")

    out = tmp_path / "site"
    showcase.assemble_showcase(out, featured_sites=sites, featured_renderer=fake_render)

    # One PNG per site, under the relocatable featured/ subdirectory.
    assert sorted(p.name for p in (out / "featured").glob("*.png")) == [
        "alpha.png",
        "bravo.png",
    ]
    assert [n for n, _ in rendered] == [3, 2]

    idx = (out / "index.html").read_text()
    assert "What SAR change looks like" in idx
    assert 'src="featured/alpha.png"' in idx
    assert "Centerfield, Utah" in idx  # baked place label, not the task codename
    # The caption states the passes, the dates and the colour semantics.
    assert "2 passes, 2024-01-01 \N{EN DASH} 2024-01-09" in idx
    assert "green = new or brighter backscatter" in idx


def test_assemble_featured_three_frames_caption(tmp_path):
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 2, 3)])
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out,
        featured_sites=sites,
        featured_frames=3,
        featured_renderer=lambda items, dest: dest.write_bytes(b"\x89PNG"),
    )
    idx = (out / "index.html").read_text()
    assert "3 passes" in idx
    assert "earliest = red, middle = green, latest = blue" in idx


def test_assemble_featured_skips_a_failed_render(tmp_path):
    """One unreadable scene must cost its own tile, not the whole showcase."""
    sites = showcase.select_featured_sites(
        [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]
    )

    def flaky(items, dest):
        if "bravo" in dest.name:
            raise RuntimeError("asset 404")
        dest.write_bytes(b"\x89PNG")

    out = tmp_path / "site"
    with pytest.warns(RuntimeWarning, match="Bravo"):
        showcase.assemble_showcase(out, featured_sites=sites, featured_renderer=flaky)

    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    idx = (out / "index.html").read_text()
    assert 'src="featured/alpha.png"' in idx
    assert "bravo.png" not in idx


def test_assemble_featured_drops_a_silent_no_op_renderer(tmp_path):
    """A renderer that writes nothing yields no tile (never a broken <img>)."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 2)])
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out, items=[_item()], featured_sites=sites, featured_renderer=lambda items, dest: None
    )
    assert "What SAR change looks like" not in (out / "index.html").read_text()


def test_assemble_without_featured_writes_no_directory(tmp_path):
    out = tmp_path / "site"
    showcase.assemble_showcase(out, items=[_item()])
    assert not (out / "featured").exists()


# --- featured narration (Mode A) ------------------------------------------
def test_assemble_bakes_featured_narration_with_injected_narrator(tmp_path):
    """Mode A: each rendered site also gets a cached reading — a JSON sidecar
    beside its composite and a summary under the tile — with no model in the
    test (the narrator is injected)."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 5)])
    narrated: list[list[str]] = []

    def fake_narrator(items):
        narrated.append([i.id for i in items])
        return {
            "summary": "The northeast brightened between the two passes.",
            "changes": ["new bright returns in the NE"],
            "item_ids": [i.id for i in items],
        }

    out = tmp_path / "site"
    showcase.assemble_showcase(
        out,
        featured_sites=sites,
        featured_renderer=lambda items, dest: dest.write_bytes(b"\x89PNG"),
        featured_narrator=fake_narrator,
    )

    # The narrator saw the site's passes, and its reading landed as a sidecar.
    assert narrated == [["Alpha-1", "Alpha-5"]]
    sidecar = out / "featured" / "alpha.narration.json"
    assert sidecar.exists()
    import json as _json

    assert _json.loads(sidecar.read_text())["summary"].startswith("The northeast")

    idx = (out / "index.html").read_text()
    assert "AI reading" in idx
    assert "The northeast brightened" in idx
    # The tile links to the full sidecar with the numbers it cites.
    assert 'href="featured/alpha.narration.json"' in idx


def test_assemble_featured_narration_none_leaves_tile_unchanged(tmp_path):
    """A narrator that declines (returns None) writes no sidecar and adds no
    reading — the tile is exactly what it would be without one."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 2)])
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out,
        featured_sites=sites,
        featured_renderer=lambda items, dest: dest.write_bytes(b"\x89PNG"),
        featured_narrator=lambda items: None,
    )
    assert not (out / "featured" / "alpha.narration.json").exists()
    idx = (out / "index.html").read_text()
    assert "AI reading" not in idx
    assert 'src="featured/alpha.png"' in idx  # the picture is still there


def test_assemble_featured_narration_failure_is_nonfatal(tmp_path):
    """A model hiccup on one site costs its reading, never its tile or the build."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 2)])

    def boom(items):
        raise RuntimeError("model timeout")

    out = tmp_path / "site"
    with pytest.warns(RuntimeWarning, match="No narration for featured site"):
        showcase.assemble_showcase(
            out,
            featured_sites=sites,
            featured_renderer=lambda items, dest: dest.write_bytes(b"\x89PNG"),
            featured_narrator=boom,
        )
    assert not (out / "featured" / "alpha.narration.json").exists()
    idx = (out / "index.html").read_text()
    assert 'src="featured/alpha.png"' in idx
    assert "AI reading" not in idx


def test_default_featured_narrator_only_for_change_view():
    """timescan (whole series) and swipe (an interactive page) have no single
    two/three-date change to read, so the default narrator is None there."""
    assert showcase._default_featured_narrator(2, view="timescan") is None
    assert showcase._default_featured_narrator(2, view="swipe") is None
    assert showcase._default_featured_narrator(2, view="change") is not None


def test_default_featured_narrator_reads_the_composite_frames(monkeypatch):
    """The baked reading is of the *same* passes the composite shows: it selects
    frames with viz.select_change_frames and hands them to narrate()."""
    picked = [_pass("Alpha", 1), _pass("Alpha", 9)]
    seen: dict = {}

    def fake_select(items, frames):
        seen["frames"] = frames
        return picked

    class _Narration:
        def to_dict(self):
            return {"summary": "ok", "item_ids": [i.id for i in picked]}

    def fake_narrate(items, **kwargs):
        seen["narrated"] = [i.id for i in items]
        seen["asset"] = kwargs.get("asset")
        seen["model"] = kwargs.get("model")
        return _Narration()

    # ``from umbra_py import narrate`` shadows the submodule with the function,
    # so reach the real module through sys.modules to patch it.
    import sys

    monkeypatch.setattr("umbra_py.viz.select_change_frames", fake_select)
    monkeypatch.setattr(sys.modules["umbra_py.narrate"], "narrate", fake_narrate)

    narrator = showcase._default_featured_narrator(3, asset="CSI", view="change", model="prov/mod")
    doc = narrator([_pass("Alpha", d) for d in (1, 5, 9)])
    assert seen["frames"] == 3
    assert seen["narrated"] == ["Alpha-1", "Alpha-9"]
    assert seen["asset"] == "CSI"
    assert seen["model"] == "prov/mod"
    assert doc == {"summary": "ok", "item_ids": ["Alpha-1", "Alpha-9"]}


# --- CLI: featured --------------------------------------------------------
def _stub_featured_renderer(monkeypatch):
    """Replace the viz-backed default renderer with one that just writes bytes."""

    def factory(frames, asset="GEC", *, view="change"):
        def render(items, dest):
            dest.write_bytes(b"\x89PNG" + str(frames).encode() + asset.encode() + view.encode())

        return render

    monkeypatch.setattr("umbra_py.showcase._default_featured_renderer", factory)


def test_cli_showcase_featured_auto_selects(tmp_path, monkeypatch):
    pool = [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: pool)
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--local", "--out", str(out), "--featured", "1", "--no-lazy-imagery"]
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    assert "featured/ (1 change artifacts)" in result.output
    assert "What SAR change looks like" in (out / "index.html").read_text()


def test_cli_showcase_featured_area_curates(tmp_path, monkeypatch):
    """--featured-area runs one search per name and takes that site's best match."""
    seen: list[str | None] = []

    def fake_gather(**kwargs):
        seen.append(kwargs.get("area"))
        if kwargs.get("area") == "Alpha":
            return [_pass("Alpha", d) for d in (1, 2)]
        return []  # the curated name that matches nothing

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", fake_gather)
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--out",
            str(out),
            "--no-explore",
            "--featured-area",
            "Alpha",
            "--featured-area",
            "Nowhere",
        ],
    )
    assert result.exit_code == 0, result.output
    # One gather per curated name, and only the matched one produced a tile.
    assert seen == ["Alpha", "Nowhere"]
    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    assert "matched --featured-area 'Nowhere'" in result.output


def test_cli_showcase_featured_zero_never_gathers_a_pool(tmp_path, monkeypatch):
    """The default (--featured 0, no --featured-area) costs nothing: no second
    search, no featured/ directory, and a page identical to before."""
    calls: list[dict] = []

    def fake_gather(**kwargs):
        calls.append(kwargs)
        return [_item()]

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", fake_gather)
    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--local", "--out", str(out), "--no-lazy-imagery"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1  # the explorer's gather only
    assert not (out / "featured").exists()
    assert "What SAR change looks like" not in (out / "index.html").read_text()


def test_cli_showcase_rejects_negative_featured(tmp_path):
    result = CliRunner().invoke(
        cli, ["showcase", "--no-explore", "--out", str(tmp_path / "s"), "--featured", "-1"]
    )
    assert result.exit_code != 0
    assert "--featured must be zero or more" in result.output


def _stub_featured_narrator(monkeypatch):
    """Replace the model-backed default narrator with one that returns a fixed
    reading, so the CLI --narrate path runs offline."""

    def factory(frames, asset="GEC", *, view="change", model=None):
        def narrate_site(items):
            return {"summary": "The site changed.", "item_ids": [i.id for i in items]}

        return narrate_site

    monkeypatch.setattr("umbra_py.showcase._default_featured_narrator", factory)


def test_cli_showcase_narrate_bakes_readings(tmp_path, monkeypatch):
    """--narrate with a key set bakes a sidecar + a reading under each tile."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        "umbra_py.cli._shared._gather_items", lambda **kwargs: [_pass("Alpha", d) for d in (1, 2)]
    )
    _stub_featured_renderer(monkeypatch)
    _stub_featured_narrator(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        ["showcase", "--local", "--out", str(out), "--no-explore", "--featured", "1", "--narrate"],
    )
    assert result.exit_code == 0, result.output
    assert (out / "featured" / "alpha.narration.json").exists()
    assert "AI reading" in (out / "index.html").read_text()


def test_cli_showcase_narrate_without_a_key_skips_but_builds(tmp_path, monkeypatch):
    """--narrate and no model key: a note, no sidecar, and the gallery still
    ships (the pictures are the showcase, the readings are the bonus)."""
    for var in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "umbra_py.cli._shared._gather_items", lambda **kwargs: [_pass("Alpha", d) for d in (1, 2)]
    )
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        ["showcase", "--local", "--out", str(out), "--no-explore", "--featured", "1", "--narrate"],
    )
    assert result.exit_code == 0, result.output
    assert "found no model API key" in result.output
    assert not (out / "featured" / "alpha.narration.json").exists()
    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]


def test_cli_showcase_narrate_noop_on_non_change_view(tmp_path, monkeypatch):
    """--narrate on a timescan view says so and bakes nothing (no single pair)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        "umbra_py.cli._shared._gather_items",
        lambda **kwargs: [_pass("Alpha", d) for d in (1, 2, 3)],
    )
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--out",
            str(out),
            "--no-explore",
            "--featured",
            "1",
            "--featured-view",
            "timescan",
            "--narrate",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "does not apply to --featured-view timescan" in result.output
    assert not list((out / "featured").glob("*.narration.json"))


def test_default_featured_renderer_calls_viz(monkeypatch, tmp_path):
    """The production renderer is the one path the injectable tests skip: pin
    that it picks evenly-spaced frames and writes a change composite."""
    calls: dict[str, object] = {}

    def fake_select(items, *, frames):
        calls["frames"] = frames
        return list(items)[:frames]

    def fake_save(items, dest, *, asset):
        calls["n"] = len(list(items))
        calls["asset"] = asset
        Path(dest).write_bytes(b"\x89PNG")
        return Path(dest)

    monkeypatch.setattr("umbra_py.viz.select_change_frames", fake_select)
    monkeypatch.setattr("umbra_py.viz.save_change_composite", fake_save)

    render = showcase._default_featured_renderer(3, asset="CSI")
    dest = tmp_path / "x.png"
    render([_pass("Alpha", d) for d in (1, 2, 3, 4)], dest)

    assert calls == {"frames": 3, "n": 3, "asset": "CSI"}
    assert dest.exists()


def test_slug_collisions_and_unsluggable_names(tmp_path):
    """Two task names that slug alike get distinct files (one must not silently
    overwrite the other's tile), and a name with no ASCII left still gets one."""
    sites = showcase.select_featured_sites(
        [
            *[_pass("Beet Piler, ND", d) for d in (1, 2)],
            *[_pass("Beet Piler ND", d) for d in (3, 4)],
            *[_pass("空港", d) for d in (5, 6)],
        ],
    )
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out, featured_sites=sites, featured_renderer=lambda items, dest: dest.write_bytes(b"x")
    )

    names = sorted(p.name for p in (out / "featured").glob("*.png"))
    assert names == ["beet-piler-nd-2.png", "beet-piler-nd.png", "site.png"]
    assert len(names) == len(sites)


# --- unified (one-page) showcase --------------------------------------------


def test_assemble_unified_writes_one_explorer_over_the_archive(tmp_path):
    """--unified collapses the map/explorer pair: the explorer reads the copied
    ``.pmtiles`` archive itself, so there is no separate viewer page to send a
    visitor to and no embedded slice to cap what they can see."""
    archive = tmp_path / "catalog.pmtiles"
    archive.write_bytes(pmtiles.build_pmtiles([_item("a"), _item("b", -111.0, 40.0)], max_zoom=3))

    out = tmp_path / "site"
    index = showcase.assemble_showcase(out, pmtiles_path=archive, unified=True)

    assert not (out / "map.html").exists()
    explore = (out / "explore.html").read_text()
    # The archive is copied in beside the page, which references it by name.
    assert (out / "catalog.pmtiles").read_bytes()[:7] == b"PMTiles"
    assert "'pmtiles://' + CFG.pmtilesUrl" in explore
    assert '"pmtilesUrl":"catalog.pmtiles"' in explore

    page = index.read_text()
    assert "Explore the whole archive" in page
    assert "explore.html" in page
    assert "map.html" not in page


def test_assemble_unified_ignores_a_gathered_slice(tmp_path):
    """Items are not the data source in unified mode, so their count must not be
    reported as the showcase's coverage."""
    archive = tmp_path / "catalog.pmtiles"
    archive.write_bytes(pmtiles.build_pmtiles([_item("a")], max_zoom=3))

    out = tmp_path / "site"
    index = showcase.assemble_showcase(
        out, items=[_item("a"), _item("b", -111.0, 40.0)], pmtiles_path=archive, unified=True
    )
    page = index.read_text()
    assert "2 acquisitions" not in page
    # ...and the slice is not embedded in the explorer either.
    assert '"features"' not in (out / "explore.html").read_text()


def test_assemble_unified_requires_an_archive(tmp_path):
    with pytest.raises(ValueError, match="pmtiles_path"):
        showcase.assemble_showcase(tmp_path / "site", items=[_item("a")], unified=True)


def test_build_showcase_explore_card_copy_depends_on_mode():
    sliced = showcase.build_showcase(explore_href="explore.html")
    whole = showcase.build_showcase(explore_href="explore.html", unified=True)
    assert "Search &amp; filter interactively" in sliced
    assert "Explore the whole archive" in whole


def test_cli_showcase_unified(tmp_path, monkeypatch):
    """The CLI flag builds the one-page showcase and never gathers a slice."""

    def _no_gather(**_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("--unified must not gather items")

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", _no_gather)
    archive = tmp_path / "catalog.pmtiles"
    archive.write_bytes(pmtiles.build_pmtiles([_item("a")], max_zoom=3))

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--unified", "--pmtiles", str(archive), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "explore.html" in result.output
    assert "map.html" not in result.output
    assert not (out / "map.html").exists()


def test_cli_showcase_unified_needs_a_basemap(tmp_path):
    result = CliRunner().invoke(cli, ["showcase", "--unified", "--out", str(tmp_path / "site")])
    assert result.exit_code != 0
    assert "--pmtiles" in result.output


def test_cli_showcase_unified_conflicts_with_no_explore(tmp_path):
    archive = tmp_path / "catalog.pmtiles"
    archive.write_bytes(pmtiles.build_pmtiles([_item("a")], max_zoom=3))
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--unified",
            "--no-explore",
            "--pmtiles",
            str(archive),
            "--out",
            str(tmp_path / "s"),
        ],
    )
    assert result.exit_code != 0
    assert "opposite" in result.output


# --- featured views: timescan + swipe -------------------------------------
def test_featured_view_table_min_passes():
    """Only the change view's requirement tracks --featured-frames; the timescan
    needs its statistical minimum and a swipe is always two."""
    views = showcase.FEATURED_VIEWS
    assert showcase.FEATURED_VIEW_NAMES == ("change", "timescan", "swipe")
    assert views["change"].min_passes_for(2) == 2
    assert views["change"].min_passes_for(3) == 3
    assert views["timescan"].min_passes_for(2) == 3
    assert views["swipe"].min_passes_for(3) == 2


def test_assemble_featured_timescan_view(tmp_path):
    """The timescan tile is a still like the change tile, but its caption counts
    the *whole* series (the renderer summarises every pass, not a selection)."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 4, 7, 9)], min_passes=3)
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out,
        featured_sites=sites,
        featured_view="timescan",
        featured_renderer=lambda items, dest: dest.write_bytes(b"\x89PNG"),
    )

    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    idx = (out / "index.html").read_text()
    assert "What a whole time series looks like" in idx
    assert 'src="featured/alpha.png"' in idx
    assert 'alt="SAR timescan composite of Alpha"' in idx
    # Every pass, not --featured-frames of them, and the timescan's own colours.
    assert "4 passes, 2024-01-01 \N{EN DASH} 2024-01-09" in idx
    assert "red = average backscatter, green = peak, blue = variability" in idx
    assert "green = new or brighter backscatter" not in idx


def test_assemble_featured_swipe_view(tmp_path):
    """A swipe map is an HTML page, so its tile is a link card rather than an
    <img> -- the one shape difference between the three views."""
    sites = showcase.select_featured_sites([_pass("Alpha", d) for d in (1, 5)])
    out = tmp_path / "site"
    showcase.assemble_showcase(
        out,
        featured_sites=sites,
        featured_view="swipe",
        featured_renderer=lambda items, dest: dest.write_text("<html>swipe</html>"),
    )

    assert [p.name for p in (out / "featured").glob("*")] == ["alpha.html"]
    idx = (out / "index.html").read_text()
    assert "Sweep between two passes" in idx
    assert 'class="shot shot--page"' in idx
    assert 'href="featured/alpha.html"' in idx
    assert "<img" not in idx  # no still exists for this view
    assert "2 passes, 2024-01-01 \N{EN DASH} 2024-01-05" in idx
    assert "drag the divider" in idx


def test_assemble_rejects_an_unknown_featured_view(tmp_path):
    with pytest.raises(ValueError, match="unknown featured_view"):
        showcase.assemble_showcase(tmp_path / "site", featured_view="nope")


def test_default_featured_renderer_timescan_calls_viz(monkeypatch, tmp_path):
    """The timescan renderer hands viz the site's *whole* series."""
    calls: dict[str, object] = {}

    def fake_save(items, dest, *, asset):
        calls["n"] = len(list(items))
        calls["asset"] = asset
        Path(dest).write_bytes(b"\x89PNG")
        return Path(dest)

    monkeypatch.setattr("umbra_py.viz.save_timescan_composite", fake_save)

    render = showcase._default_featured_renderer(2, asset="GEC", view="timescan")
    dest = tmp_path / "x.png"
    render([_pass("Alpha", d) for d in (1, 2, 3, 4)], dest)

    assert calls == {"n": 4, "asset": "GEC"}
    assert dest.exists()


def test_default_featured_renderer_swipe_calls_viz(monkeypatch, tmp_path):
    """The swipe renderer reuses select_change_frames' two-frame pick, so the
    swipe and change views tell the same story about a site."""
    calls: dict[str, object] = {}

    def fake_select(items, *, frames):
        calls["frames"] = frames
        items = list(items)
        return [items[0], items[-1]]

    def fake_save(before, after, dest, *, asset):
        calls["pair"] = (before.id, after.id)
        calls["asset"] = asset
        Path(dest).write_text("<html/>")
        return Path(dest)

    monkeypatch.setattr("umbra_py.viz.select_change_frames", fake_select)
    monkeypatch.setattr("umbra_py.viz.save_swipe_map", fake_save)

    render = showcase._default_featured_renderer(3, asset="CSI", view="swipe")
    dest = tmp_path / "x.html"
    render([_pass("Alpha", d) for d in (1, 2, 3)], dest)

    assert calls == {"frames": 2, "pair": ("Alpha-1", "Alpha-3"), "asset": "CSI"}
    assert dest.exists()


def test_cli_showcase_featured_view_timescan_needs_three_passes(tmp_path, monkeypatch):
    """--featured-view timescan raises the bar a site must clear before any
    render is attempted: a two-pass site can't be summarised statistically."""
    pool = [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: pool)
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--out",
            str(out),
            "--no-explore",
            "--featured",
            "5",
            "--featured-view",
            "timescan",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    assert "featured/ (1 timescan artifacts)" in result.output
    assert "What a whole time series looks like" in (out / "index.html").read_text()


def test_cli_showcase_featured_view_swipe(tmp_path, monkeypatch):
    pool = [_pass("Alpha", d) for d in (1, 2)]
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: pool)
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli,
        [
            "showcase",
            "--local",
            "--out",
            str(out),
            "--no-explore",
            "--featured",
            "1",
            "--featured-view",
            "swipe",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in (out / "featured").glob("*")] == ["alpha.html"]
    assert "featured/ (1 swipe artifacts)" in result.output
    assert 'href="featured/alpha.html"' in (out / "index.html").read_text()
