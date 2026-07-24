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

Design, in the repo's grain:

* **Static, no server, no extra.** Every file it writes is self-contained HTML
  (the landing page has no CDN dependency at all; the map/explorer reuse the same
  pinned CDNs the underlying commands already use). The output directory drops
  straight onto any static host -- the ``.github/workflows/docs.yml`` Pages deploy
  copies it into ``site/showcase/`` next to the mkdocs build.
* **Deterministic and offline-testable.** :func:`build_showcase` is a pure
  string builder and :func:`assemble_showcase` only copies files and calls the
  existing ``save_*`` writers, so the whole feature is covered without a network
  or a ``viz`` extra (the CLI does the one networked step -- fetching the
  published snapshot -- outside this module).
* **License propagation.** The mandatory CC-BY attribution rides on the landing
  page, exactly as it does on every other visual artifact.

Was ``DEMO_APP_GAPS.md`` G7 / ``STRATEGY.md`` §8's "GitHub Pages deploy of the
static ``umbra demo`` / ``catalog.pmtiles`` showcase".
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
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
) -> str:
    """Render the showcase landing page as a self-contained HTML string.

    The page is a small, dependency-free hero + card grid tying together the
    artifacts :func:`assemble_showcase` writes. Each card is emitted only when
    its target exists, so a metadata-only build (no ``map_href``) or an
    explorer-less build (no ``explore_href``) still produces a coherent page.

    Parameters
    ----------
    map_href, explore_href:
        Relative links to the whole-catalog map viewer and the interactive
        explorer (typically ``"map.html"`` / ``"explore.html"``). ``None`` drops
        that card.
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
    if explore_href:
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


def assemble_showcase(
    dest_dir: str | os.PathLike,
    *,
    items: Iterable[UmbraItem] | None = None,
    pmtiles_path: str | os.PathLike | None = None,
    viewer_title: str | None = None,
    demo_kwargs: dict[str, Any] | None = None,
    **showcase_kwargs: Any,
) -> Path:
    """Assemble a static showcase directory and return its ``index.html`` path.

    Writes, into ``dest_dir`` (created if absent):

    * ``map.html`` + a copy of the ``.pmtiles`` archive -- only when
      ``pmtiles_path`` is given (the MapLibre viewer over the whole-catalog
      basemap).
    * ``explore.html`` -- only when ``items`` is given and non-empty (the
      ``umbra demo`` interactive explorer).
    * ``index.html`` -- always (the landing page, with cards for whichever of the
      above were written).

    Parameters
    ----------
    items:
        Acquisitions for the explorer. ``None`` or empty skips ``explore.html``
        and its card.
    pmtiles_path:
        A local whole-catalog ``.pmtiles`` file to include. It is copied into
        ``dest_dir`` (so the directory is self-contained and relocatable) and the
        viewer references it by name. ``None`` skips ``map.html`` and its card.
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
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    item_list = list(items) if items is not None else None

    map_href: str | None = None
    if pmtiles_path is not None:
        from .pmtiles import save_viewer  # noqa: PLC0415

        src = Path(pmtiles_path)
        copied = dest / src.name
        # Copy the archive in beside the viewer unless it is already there (a
        # caller may hand us a path that is already inside dest_dir).
        if copied.resolve() != src.resolve():
            shutil.copyfile(src, copied)
        save_viewer(
            copied.name,
            dest / "map.html",
            title=viewer_title or showcase_kwargs.get("title", DEFAULT_TITLE),
        )
        map_href = "map.html"

    explore_href: str | None = None
    if item_list:
        from .demo import save_demo  # noqa: PLC0415

        save_demo(item_list, dest / "explore.html", **(demo_kwargs or {}))
        explore_href = "explore.html"

    index = dest / "index.html"
    index.write_text(
        build_showcase(
            map_href=map_href,
            explore_href=explore_href,
            item_count=len(item_list) if item_list is not None else None,
            **showcase_kwargs,
        )
    )
    return index


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
