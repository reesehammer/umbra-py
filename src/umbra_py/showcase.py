"""Static, hostable showcase site (the ``umbra showcase`` command).

The demo-gap analysis (:doc:`DEMO_APP_GAPS`) closed almost every piece of a
full-catalog interactive demo one command at a time: the whole-archive PMTiles
basemap (``umbra tiles``), the self-serve interactive explorer (``umbra demo``),
the published index/basemap snapshots a fresh install fetches with no crawl. The
one gap it kept flagging as *the remaining G7 piece* was a place to **put** them:
a static site, hostable on GitHub Pages beside the docs, that a curious analyst
opens with zero install and lands on a whole-archive map and a searchable
explorer. That is what this module assembles.

It is deliberately a *composer*, not a new renderer. It reuses the two artifacts
the toolkit already produces and ties them together with a small self-contained
landing page:

* ``map.html`` -- the MapLibre GL viewer over the whole-catalog ``catalog.pmtiles``
  basemap (:func:`umbra_py.pmtiles.save_viewer`), with the ``.pmtiles`` archive
  copied in beside it so the whole directory is relocatable.
* ``explore.html`` -- the interactive ``umbra demo`` explorer
  (:func:`umbra_py.demo.save_demo`) over a gathered slice of the catalog.
* ``index.html`` -- the landing page :func:`build_showcase` renders, linking the
  two above plus the install command, the docs and the source.
* **or one page instead of two** (``umbra showcase --unified``): the explorer
  now reads a tiled archive directly (:func:`umbra_py.demo.build_demo`'s
  ``pmtiles_url``), so ``explore.html`` can be built *over* ``catalog.pmtiles``
  and ``map.html`` dropped. A visitor lands on a single page covering every
  acquisition **with** the filters, instead of choosing between a whole-catalog
  map they can only click and a filterable explorer over a slice.
* ``featured/*.png`` -- optional **precomputed change composites** for a handful
  of repeat-imaged sites (:func:`select_featured_sites`), shown as a gallery on
  the landing page. A first-time visitor sees *what SAR change looks like*
  immediately, with no render round-trip and no server; the explorer is still
  there for anything beyond the marquee set.

Design, in the repo's grain:

* **Static, no server, no extra.** Every file it writes is self-contained HTML
  (the landing page has no CDN dependency at all; the map/explorer reuse the same
  pinned CDNs the underlying commands already use). The output directory drops
  straight onto any static host -- the ``.github/workflows/docs.yml`` Pages deploy
  copies it into ``site/showcase/`` next to the mkdocs build.
* **Deterministic and offline-testable.** :func:`build_showcase` and
  :func:`select_featured_sites` are pure, and :func:`assemble_showcase` only
  copies files and calls the existing ``save_*`` writers -- the one step that
  needs the ``viz`` extra (rendering a featured composite) goes through an
  injectable ``featured_renderer``, so the whole feature is covered without a
  network or a ``viz`` extra (the CLI does the other networked step -- fetching
  the published snapshot -- outside this module).
* **License propagation.** The mandatory CC-BY attribution rides on the landing
  page, exactly as it does on every other visual artifact.

Was ``DEMO_APP_GAPS.md`` G7 / ``STRATEGY.md`` §8's "GitHub Pages deploy of the
static ``umbra demo`` / ``catalog.pmtiles`` showcase" (and, for the featured
gallery, its "precompute showcase artifacts for ~6-10 curated sites" R4
follow-on).
"""

from __future__ import annotations

import os
import re
import shutil
import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from .constants import ATTRIBUTION, GITHUB_REPO
from .models import UmbraItem

#: Default landing-page copy. The pitch is the strategy thesis in one line --
#: "the open data, searchable and previewable with no install".
DEFAULT_TITLE = "Umbra open-data SAR"
DEFAULT_TAGLINE = (
    "Browse and search Umbra's open SAR archive from your browser -- "
    "no account, no install, no data download."
)

#: Project URLs the landing page links to. Derived from the one repo constant so
#: a fork inherits its own links; the docs URL matches ``mkdocs.yml``'s
#: ``site_url`` (GitHub Pages default for the repo).
_OWNER, _NAME = GITHUB_REPO.split("/", 1)
DEFAULT_REPO_URL = f"https://github.com/{GITHUB_REPO}"
DEFAULT_DOCS_URL = f"https://{_OWNER}.github.io/{_NAME}/"

#: Default number of sites the featured gallery precomputes, and the number of
#: passes composited into each. Two frames is the two-colour change view
#: (green/magenta), which reads at thumbnail size; three is the temporal RGB.
DEFAULT_FEATURED_COUNT = 6
DEFAULT_FEATURED_FRAMES = 2

#: Subdirectory the featured composites are written to, relative to the
#: showcase root (so the whole directory stays relocatable).
FEATURED_DIR = "featured"


@dataclass(frozen=True)
class FeaturedSite:
    """A repeat-imaged site chosen for a precomputed change composite.

    ``items`` is every usable pass of the site, oldest-first; the renderer
    narrows it to the frames it composites (via
    :func:`umbra_py.viz.select_change_frames`), so a caller can also use the
    full series for a time-lapse.
    """

    task: str
    items: list[UmbraItem]

    @property
    def label(self) -> str:
        """Human-readable site name: the baked place label when the index has
        one (``umbra index bake``), else the task codename."""
        for item in self.items:
            if item.place:
                return item.place
        return self.task

    @property
    def slug(self) -> str:
        """Filesystem/URL-safe stem for this site's artifact."""
        return _slug(self.task)

    @property
    def date_range(self) -> str | None:
        """``"2024-01-05 - 2024-03-11"`` over the site's passes (a single date
        when they share one), or ``None`` if nothing is dated."""
        dates = sorted({i.datetime.date().isoformat() for i in self.items if i.datetime})
        if not dates:
            return None
        return dates[0] if len(dates) == 1 else f"{dates[0]} \N{EN DASH} {dates[-1]}"


@dataclass(frozen=True)
class FeaturedArtifact:
    """One rendered tile in the landing page's featured gallery."""

    href: str
    label: str
    caption: str


#: Renders one site's featured artifact to a path. Injectable so the whole
#: assembler stays offline-testable; the default calls into ``umbra_py.viz``.
FeaturedRenderer = Callable[[list[UmbraItem], Path], None]


def _slug(value: str) -> str:
    """Lowercase, ASCII-ish, hyphen-separated stem for a task name.

    Umbra task names carry spaces, commas and non-ASCII characters
    (``"Centerfield, Utah"``); the artifact filename ends up in a URL on a
    static host, so reduce it to an unambiguous slug.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned or "site"


def select_featured_sites(
    items: Iterable[UmbraItem],
    *,
    count: int = DEFAULT_FEATURED_COUNT,
    min_passes: int = 2,
) -> list[FeaturedSite]:
    """Choose the most repeat-imaged sites in ``items`` for the featured gallery.

    A change composite needs two or more dated passes over the *same* ground,
    and Umbra files every pass of a site under one task directory -- so the
    tasks with the most acquisitions in the candidate pool are exactly the ones
    worth precomputing. Sites are ranked by pass count (descending) and then by
    task name, so the selection is deterministic for a given pool rather than
    dependent on iteration order.

    Parameters
    ----------
    items:
        The candidate pool (e.g. a ``--local`` search). Items with no task or no
        datetime can't be grouped or ordered, so they are dropped.
    count:
        Maximum number of sites to return.
    min_passes:
        Passes a site needs before it qualifies (2 is the minimum a change
        composite can use).

    Returns the sites best-first, each carrying its passes oldest-first.
    Deterministic and dependency-free -- it calls no renderer and no model.
    """
    if count <= 0:
        return []
    by_task: dict[str, list[UmbraItem]] = {}
    for item in items:
        if item.task and item.datetime is not None:
            by_task.setdefault(item.task, []).append(item)

    # Every pass here has a datetime (filtered above); the ``or datetime.min``
    # fallback is unreachable but keeps the sort key typed, as in viz.py.
    sites = [
        FeaturedSite(task=task, items=sorted(passes, key=lambda i: i.datetime or datetime.min))
        for task, passes in by_task.items()
        if len(passes) >= min_passes
    ]
    sites.sort(key=lambda s: (-len(s.items), s.task))
    return sites[:count]


def build_showcase(
    *,
    map_href: str | None = None,
    explore_href: str | None = None,
    title: str = DEFAULT_TITLE,
    tagline: str = DEFAULT_TAGLINE,
    item_count: int | None = None,
    updated: str | None = None,
    repo_url: str = DEFAULT_REPO_URL,
    docs_url: str = DEFAULT_DOCS_URL,
    featured: Sequence[FeaturedArtifact] = (),
    unified: bool = False,
) -> str:
    """Render the showcase landing page as a self-contained HTML string.

    The page is a small, dependency-free hero + optional featured gallery + card
    grid tying together the artifacts :func:`assemble_showcase` writes. Each
    card is emitted only when its target exists, so a metadata-only build (no
    ``map_href``) or an explorer-less build (no ``explore_href``) still produces
    a coherent page.

    Parameters
    ----------
    map_href, explore_href:
        Relative links to the whole-catalog map viewer and the interactive
        explorer (typically ``"map.html"`` / ``"explore.html"``). ``None`` drops
        that card.
    unified:
        Set when ``explore_href`` points at an explorer built over the *whole*
        tiled archive rather than a gathered slice (``umbra showcase
        --unified``). It only changes that card's copy — there is no separate
        map page to send people to, because the explorer is one.
    featured:
        Precomputed change composites to show above the cards. Empty (the
        default) drops the whole gallery section, so a metadata-only showcase is
        byte-identical to one built before this option existed.
    title, tagline:
        Hero heading and one-line pitch.
    item_count:
        Number of acquisitions the explorer covers, shown in the stats line when
        given.
    updated:
        A freshness stamp for the underlying snapshot (e.g. the index
        ``built_at``), shown in the stats line when given.
    repo_url, docs_url:
        Links for the "source" and "docs" cards and the footer; default to this
        project's GitHub repo and Pages site.

    Returns the HTML as a string; use :func:`assemble_showcase` (or write it
    yourself) to place it on disk.
    """
    cards: list[str] = []
    if map_href:
        cards.append(
            _card(
                map_href,
                "Map the whole archive",
                "A zoomable basemap of every acquisition in the open catalog. "
                "Click a scene for its details and product links.",
                "\N{WORLD MAP}",
            )
        )
    if explore_href and unified:
        cards.append(
            _card(
                explore_href,
                "Explore the whole archive",
                "Every acquisition in the open catalog on one zoomable map, with "
                "live filters for place, date range and product type \N{EM DASH} "
                "no install, no search round-trip.",
                "\N{LEFT-POINTING MAGNIFYING GLASS}",
            )
        )
    elif explore_href:
        cards.append(
            _card(
                explore_href,
                "Search &amp; filter interactively",
                "Filter by place, date range and product type; cluster markers "
                "scale past a plain map, and any scene streams its SAR quicklook "
                "on click.",
                "\N{LEFT-POINTING MAGNIFYING GLASS}",
            )
        )
    cards.append(
        _card(
            escape(docs_url),
            "Read the docs",
            "Install the toolkit and go from a search to an analysis-ready array "
            "in a few lines of Python or one CLI call.",
            "\N{OPEN BOOK}",
        )
    )
    cards.append(
        _card(
            escape(repo_url),
            "Get the source",
            "<code>pip install umbra-py</code> \N{EM DASH} an open-source, "
            "Python-first toolkit for Umbra's open SAR data.",
            "\N{PACKAGE}",
        )
    )

    stats = _stats_line(item_count, updated)
    return _PAGE_TEMPLATE.format(
        title=escape(title),
        tagline=escape(tagline),
        styles=_STYLES,
        stats=stats,
        featured=_featured_section(featured),
        cards="\n".join(cards),
        attribution=escape(ATTRIBUTION),
        repo_url=escape(repo_url),
        docs_url=escape(docs_url),
    )


def _card(href: str, heading: str, body: str, icon: str) -> str:
    """One landing-page card. ``href`` is a relative path or an already-escaped
    absolute URL; ``heading``/``body`` may carry the small amount of trusted
    inline markup used above (``&amp;``, ``<code>``) and are otherwise literal."""
    return (
        f'      <a class="card" href="{escape(href, quote=True)}">\n'
        f'        <span class="icon" aria-hidden="true">{icon}</span>\n'
        f"        <h2>{heading}</h2>\n"
        f"        <p>{body}</p>\n"
        f"      </a>"
    )


def _stats_line(item_count: int | None, updated: str | None) -> str:
    """Render the optional "N acquisitions - updated X" line (empty when neither
    fact is known, so a bare showcase has no dangling separator)."""
    parts: list[str] = []
    if item_count is not None:
        noun = "acquisition" if item_count == 1 else "acquisitions"
        parts.append(f"{item_count:,} {noun}")
    if updated:
        parts.append(f"updated {escape(updated)}")
    if not parts:
        return ""
    return '    <p class="stats">' + " &middot; ".join(parts) + "</p>"


def _featured_section(featured: Sequence[FeaturedArtifact]) -> str:
    """Render the "what SAR change looks like" gallery (empty string when there
    is nothing to show, so the page has no dangling heading).

    Each tile links to its own full-size PNG -- deliberately not to a render
    endpoint, so the section keeps working on a plain static host.
    """
    if not featured:
        return ""
    figures = "\n".join(
        f'        <figure class="shot">\n'
        f'          <a href="{escape(art.href, quote=True)}">'
        f'<img src="{escape(art.href, quote=True)}" '
        f'alt="SAR change composite of {escape(art.label, quote=True)}" '
        f'loading="lazy"/></a>\n'
        f"          <figcaption><strong>{escape(art.label)}</strong>"
        f"<span>{escape(art.caption)}</span></figcaption>\n"
        f"        </figure>"
        for art in featured
    )
    return (
        '    <section class="featured">\n'
        "      <h2>What SAR change looks like</h2>\n"
        '      <p class="lede">Each image composites repeat passes of one site onto a '
        "shared grid: ground that stayed put reads gray, and anything that appeared "
        "or vanished between passes is tinted by when it happened. Rendered ahead of "
        "time from the open catalog \N{EM DASH} no account, no download.</p>\n"
        '      <div class="shots">\n'
        f"{figures}\n"
        "      </div>\n"
        "    </section>"
    )


def featured_caption(site: FeaturedSite, frames: int) -> str:
    """One line of honest provenance under a featured tile: how many passes were
    composited, over what dates, and what the colours mean.

    The colour semantics come straight from :func:`umbra_py.viz.change_composite`
    (two dates -> green/magenta, three -> temporal RGB); keep them in step.
    """
    colors = (
        "green = new or brighter backscatter, magenta = gone or dimmer"
        if frames == 2
        else "earliest = red, middle = green, latest = blue"
    )
    span = site.date_range
    passes = f"{frames} passes" if frames != 1 else "1 pass"
    return f"{passes}, {span} \N{EM DASH} {colors}." if span else f"{passes} \N{EM DASH} {colors}."


def assemble_showcase(
    dest_dir: str | os.PathLike,
    *,
    items: Iterable[UmbraItem] | None = None,
    pmtiles_path: str | os.PathLike | None = None,
    unified: bool = False,
    viewer_title: str | None = None,
    demo_kwargs: dict[str, Any] | None = None,
    featured_sites: Sequence[FeaturedSite] = (),
    featured_frames: int = DEFAULT_FEATURED_FRAMES,
    featured_renderer: FeaturedRenderer | None = None,
    **showcase_kwargs: Any,
) -> Path:
    """Assemble a static showcase directory and return its ``index.html`` path.

    Writes, into ``dest_dir`` (created if absent):

    * ``map.html`` + a copy of the ``.pmtiles`` archive -- only when
      ``pmtiles_path`` is given (the MapLibre viewer over the whole-catalog
      basemap). With ``unified=True`` the archive is still copied in but
      ``map.html`` is not written: the explorer below covers it.
    * ``explore.html`` -- the ``umbra demo`` interactive explorer: over the
      copied archive when ``unified=True``, otherwise over ``items`` (and
      skipped when those are absent or empty).
    * ``featured/<slug>.png`` -- one precomputed change composite per entry in
      ``featured_sites`` (none by default).
    * ``index.html`` -- always (the landing page, with cards for whichever of the
      above were written).

    Parameters
    ----------
    items:
        Acquisitions for the explorer. ``None`` or empty skips ``explore.html``
        and its card.
    featured_sites:
        Sites (from :func:`select_featured_sites`) to precompute a change
        composite for. Empty (the default) leaves the page and the output
        directory exactly as they were before this option existed.
    featured_frames:
        Passes composited into each featured image -- 2 (green/magenta) or 3
        (temporal RGB), per :func:`umbra_py.viz.change_composite`.
    featured_renderer:
        ``(items, dest) -> None``, called once per featured site. Defaults to
        :func:`umbra_py.viz.save_change_composite` over the frames
        :func:`umbra_py.viz.select_change_frames` picks, which needs the ``viz``
        extra and streams each scene's overview. A site whose render fails is
        warned about and dropped, never fatal: one unreadable asset must not
        cost the whole showcase.
    pmtiles_path:
        A local whole-catalog ``.pmtiles`` file to include. It is copied into
        ``dest_dir`` (so the directory is self-contained and relocatable) and the
        viewer references it by name. ``None`` skips ``map.html`` and its card.
    unified:
        Build **one** page instead of two. The showcase's map and explorer have
        always been siblings — a whole-catalog viewer you can only click, and an
        explorer with real filters over a gathered slice — because the explorer
        had no way to read a tiled archive. It has one now
        (:func:`umbra_py.demo.build_demo`'s ``pmtiles_url``), so ``unified=True``
        builds ``explore.html`` *over the copied archive* and drops ``map.html``:
        the landing page sends a visitor to a single explorer covering every
        acquisition, with the filters. Requires ``pmtiles_path``; ``items`` is
        unused in this mode (the archive is the data source).
    viewer_title:
        Title for the map viewer page (defaults to the landing-page title).
    demo_kwargs:
        Extra keyword arguments forwarded to :func:`umbra_py.demo.save_demo`
        (e.g. ``asset``, ``lazy_imagery``, ``subtitle``, ``server_url``).
    **showcase_kwargs:
        Forwarded to :func:`build_showcase` (``title``, ``tagline``, ``updated``,
        ``repo_url``, ``docs_url``). ``item_count`` and the two ``*_href`` values
        are supplied here from what was actually written.

    Deterministic and offline: it only copies a file and calls the existing
    ``save_viewer`` / ``save_demo`` writers, so it needs no network and no
    ``viz`` extra.
    """
    if unified and pmtiles_path is None:
        raise ValueError("unified=True needs a pmtiles_path (the archive is the data source)")

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    item_list = list(items) if items is not None else None

    map_href: str | None = None
    archive_name: str | None = None
    if pmtiles_path is not None:
        src = Path(pmtiles_path)
        copied = dest / src.name
        # Copy the archive in beside the page that reads it unless it is already
        # there (a caller may hand us a path that is already inside dest_dir).
        if copied.resolve() != src.resolve():
            shutil.copyfile(src, copied)
        archive_name = copied.name
        if not unified:
            from .pmtiles import save_viewer  # noqa: PLC0415

            save_viewer(
                archive_name,
                dest / "map.html",
                title=viewer_title or showcase_kwargs.get("title", DEFAULT_TITLE),
            )
            map_href = "map.html"

    explore_href: str | None = None
    if unified:
        from .demo import save_demo  # noqa: PLC0415

        # One page over the whole tiled archive: no item list, no map sibling.
        save_demo([], dest / "explore.html", pmtiles_url=archive_name, **(demo_kwargs or {}))
        explore_href = "explore.html"
        # The gathered slice is not what the page shows, so its size must not be
        # reported as the showcase's coverage.
        item_list = None
    elif item_list:
        from .demo import save_demo  # noqa: PLC0415

        save_demo(item_list, dest / "explore.html", **(demo_kwargs or {}))
        explore_href = "explore.html"

    featured = _render_featured(
        dest,
        featured_sites,
        frames=featured_frames,
        renderer=featured_renderer,
    )

    index = dest / "index.html"
    index.write_text(
        build_showcase(
            map_href=map_href,
            explore_href=explore_href,
            unified=unified,
            item_count=len(item_list) if item_list is not None else None,
            featured=featured,
            **showcase_kwargs,
        )
    )
    return index


def _render_featured(
    dest: Path,
    sites: Sequence[FeaturedSite],
    *,
    frames: int,
    renderer: FeaturedRenderer | None,
) -> list[FeaturedArtifact]:
    """Render one change composite per site into ``dest/featured/`` and return
    the artifacts that actually landed (a failed render is skipped, not fatal)."""
    if not sites:
        return []
    render = renderer if renderer is not None else _default_featured_renderer(frames)
    out_dir = dest / FEATURED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[FeaturedArtifact] = []
    used: set[str] = set()
    for site in sites:
        # Two task names can slug identically ("Beet Piler, ND" / "Beet Piler
        # ND"); suffix rather than let one site silently overwrite the other's
        # tile and show the same image twice.
        stem = site.slug
        while stem in used:
            stem = f"{site.slug}-{len(used) + 1}"
        used.add(stem)

        png = out_dir / f"{stem}.png"
        try:
            render(site.items, png)
        except Exception as exc:  # noqa: BLE001 - one bad scene must not fail the build
            warnings.warn(
                f"Skipping featured site {site.task!r}: {exc}", RuntimeWarning, stacklevel=2
            )
            continue
        if not png.exists():  # a renderer that silently wrote nothing
            continue
        artifacts.append(
            FeaturedArtifact(
                href=f"{FEATURED_DIR}/{png.name}",
                label=site.label,
                caption=featured_caption(site, min(frames, len(site.items))),
            )
        )
    return artifacts


def _default_featured_renderer(frames: int, asset: str = "GEC") -> FeaturedRenderer:
    """The production renderer: co-register the evenly-spaced frames of a site
    and write the change composite. Needs the ``viz`` extra, and streams only a
    downsampled overview of each scene."""

    def render(items: list[UmbraItem], dest: Path) -> None:
        from .viz import save_change_composite, select_change_frames  # noqa: PLC0415

        save_change_composite(select_change_frames(items, frames=frames), dest, asset=asset)

    return render


_STYLES = """
    :root {
      color-scheme: light dark;
      --bg: #0b1020;
      --panel: #141a2e;
      --fg: #eef2ff;
      --muted: #9aa6c7;
      --accent: #7c9cff;
      --border: #263156;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f5f7ff; --panel: #ffffff; --fg: #131b3a;
        --muted: #55607f; --accent: #3757d6; --border: #dde3f5;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh; background: var(--bg); color: var(--fg);
      font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      display: flex; flex-direction: column; align-items: center;
    }
    main { width: 100%; max-width: 960px; padding: clamp(1.5rem, 5vw, 4rem) 1.25rem; }
    header { text-align: center; margin-bottom: 2.5rem; }
    h1 { font-size: clamp(2rem, 6vw, 3rem); margin: 0 0 .5rem; letter-spacing: -.02em; }
    .tagline { color: var(--muted); font-size: 1.15rem; max-width: 40ch; margin: 0 auto; }
    .stats { color: var(--muted); font-size: .95rem; margin: 1rem 0 0; }
    .grid {
      display: grid; gap: 1rem;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    }
    .card {
      display: block; text-decoration: none; color: inherit;
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 14px; padding: 1.5rem;
      transition: transform .12s ease, border-color .12s ease;
    }
    .card:hover { transform: translateY(-3px); border-color: var(--accent); }
    .card .icon { font-size: 1.75rem; display: block; margin-bottom: .5rem; }
    .card h2 { font-size: 1.15rem; margin: 0 0 .4rem; }
    .card p { color: var(--muted); font-size: .95rem; margin: 0; }
    .card code { background: rgba(124,156,255,.15); padding: .05em .35em; border-radius: 5px; }
    .featured { margin: 0 0 2.5rem; }
    .featured h2 { font-size: 1.4rem; margin: 0 0 .4rem; }
    .featured .lede {
      color: var(--muted); font-size: .95rem; margin: 0 0 1.25rem; max-width: 62ch;
    }
    .shots {
      display: grid; gap: 1rem;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    }
    .shot {
      margin: 0; background: var(--panel); border: 1px solid var(--border);
      border-radius: 14px; overflow: hidden;
    }
    .shot img {
      display: block; width: 100%; aspect-ratio: 1; object-fit: cover;
      background: #000;
    }
    .shot figcaption { padding: .75rem 1rem 1rem; font-size: .85rem; }
    .shot figcaption strong { display: block; margin-bottom: .15rem; }
    .shot figcaption span { color: var(--muted); }
    footer {
      color: var(--muted); font-size: .85rem; text-align: center;
      padding: 0 1.25rem 2.5rem; max-width: 640px;
    }
    footer a { color: var(--accent); }
""".rstrip()

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{title}</title>
  <style>{styles}</style>
</head>
<body>
  <main>
    <header>
      <h1>{title}</h1>
      <p class="tagline">{tagline}</p>
{stats}
    </header>
{featured}
    <div class="grid">
{cards}
    </div>
  </main>
  <footer>
    <p>{attribution}
    Not affiliated with or endorsed by Umbra Lab, Inc.</p>
    <p><a href="{docs_url}">Documentation</a> &middot;
    <a href="{repo_url}">Source on GitHub</a></p>
  </footer>
</body>
</html>
"""
