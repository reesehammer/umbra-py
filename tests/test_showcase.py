"""Tests for the ``umbra showcase`` static-site composer.

The showcase is a *composer*: it reuses the demo explorer and the PMTiles viewer
the toolkit already produces and ties them together with a landing page. So the
contract to pin down is (1) the landing page carries the right links, stats and
attribution and drops the cards it has no target for, and (2) the assembler
writes exactly the files the inputs justify and copies the basemap in beside its
viewer. It is stdlib-only, so none of this needs a network or the viz extra.
"""

from __future__ import annotations

from pathlib import Path

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
