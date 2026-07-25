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
from umbra_py.models import UmbraItem


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
    # Project links default to this repo / its Pages docs.
    assert "github.com/reesehammer/umbra-py" in html
    assert "reesehammer.github.io/umbra-py" in html


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
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: items)
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

    monkeypatch.setattr("umbra_py.cli._gather_items", _boom)
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
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_item()])
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
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_item()])
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


# --- CLI: featured --------------------------------------------------------
def _stub_featured_renderer(monkeypatch):
    """Replace the viz-backed default renderer with one that just writes bytes."""

    def factory(frames, asset="GEC"):
        def render(items, dest):
            dest.write_bytes(b"\x89PNG" + str(frames).encode() + asset.encode())

        return render

    monkeypatch.setattr("umbra_py.showcase._default_featured_renderer", factory)


def test_cli_showcase_featured_auto_selects(tmp_path, monkeypatch):
    pool = [*[_pass("Alpha", d) for d in (1, 2, 3)], *[_pass("Bravo", d) for d in (4, 5)]]
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: pool)
    _stub_featured_renderer(monkeypatch)

    out = tmp_path / "site"
    result = CliRunner().invoke(
        cli, ["showcase", "--local", "--out", str(out), "--featured", "1", "--no-lazy-imagery"]
    )
    assert result.exit_code == 0, result.output
    assert [p.name for p in (out / "featured").glob("*.png")] == ["alpha.png"]
    assert "featured/ (1 composites)" in result.output
    assert "What SAR change looks like" in (out / "index.html").read_text()


def test_cli_showcase_featured_area_curates(tmp_path, monkeypatch):
    """--featured-area runs one search per name and takes that site's best match."""
    seen: list[str | None] = []

    def fake_gather(**kwargs):
        seen.append(kwargs.get("area"))
        if kwargs.get("area") == "Alpha":
            return [_pass("Alpha", d) for d in (1, 2)]
        return []  # the curated name that matches nothing

    monkeypatch.setattr("umbra_py.cli._gather_items", fake_gather)
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

    monkeypatch.setattr("umbra_py.cli._gather_items", fake_gather)
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

    monkeypatch.setattr("umbra_py.cli._gather_items", _no_gather)
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
