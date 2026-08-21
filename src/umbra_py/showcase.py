"""Static, hostable showcase site (the ``umbra showcase`` command).

The demo-gap analysis closed almost every piece of a
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
* ``featured/*`` -- optional **precomputed artifacts** for a handful of
  repeat-imaged sites (:func:`select_featured_sites`), shown as a gallery on the
  landing page. A first-time visitor sees *what SAR change looks like*
  immediately, with no render round-trip and no server; the explorer is still
  there for anything beyond the marquee set. Three views of the same marquee set
  are available (:data:`FEATURED_VIEWS`): the two-or-three-date ``change``
  composite, the whole-series ``timescan`` composite, and the interactive
  before/after ``swipe`` map.

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

Was the G7 gap / ``docs/STRATEGY.md`` §8's "GitHub Pages deploy of the
static ``umbra demo`` / ``catalog.pmtiles`` showcase" (and, for the featured
gallery, its "precompute showcase artifacts for ~6-10 curated sites" R4
follow-on).
"""

from __future__ import annotations

import json
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

from .catalog import DateLike, _coerce_date
from .constants import ATTRIBUTION, DOCS_URL, GITHUB_REPO
from .models import UmbraItem

#: Default landing-page copy. The pitch is the strategy thesis in one line --
#: "the open data, searchable and previewable with no install".
DEFAULT_TITLE = "Umbra open-data SAR"
DEFAULT_TAGLINE = (
    "Browse and search Umbra's open SAR archive from your browser -- "
    "no account, no install, no data download."
)

#: Project URLs the landing page links to. The repo URL is derived from
#: :data:`~umbra_py.constants.GITHUB_REPO` so a fork inherits its own source
#: link; the docs URL is the published site (:data:`~umbra_py.constants.DOCS_URL`),
#: which must stay in step with ``mkdocs.yml`` ``site_url``.
DEFAULT_REPO_URL = f"https://github.com/{GITHUB_REPO}"
DEFAULT_DOCS_URL = DOCS_URL

#: Default number of sites the featured gallery precomputes, and the number of
#: passes composited into each. Two frames is the two-colour change view
#: (green/magenta), which reads at thumbnail size; three is the temporal RGB.
DEFAULT_FEATURED_COUNT = 6
DEFAULT_FEATURED_FRAMES = 2

#: Subdirectory the featured composites are written to, relative to the
#: showcase root (so the whole directory stays relocatable).
FEATURED_DIR = "featured"


@dataclass(frozen=True)
class FeaturedView:
    """One way of precomputing a marquee site's story for the landing page.

    The featured gallery started as a single view -- a change composite per site
    -- but the same selection feeds two more renderings of the same data, and
    they differ in exactly the four ways this record captures: what the renderer
    writes (``suffix``), how many passes a site needs to qualify
    (``min_passes``), how the tile is shaped (``kind``: a still ``"image"`` or a
    link card for a ``"page"``), and what the section says about it.

    Attributes
    ----------
    name:
        The view's CLI name (``umbra showcase --featured-view``).
    suffix:
        Extension of the artifact the renderer writes (``".png"`` for a still,
        ``".html"`` for an interactive page).
    kind:
        ``"image"`` -- the tile is the picture itself; ``"page"`` -- the tile is
        a link card, because there is no still to show.
    min_passes:
        Passes a site needs before the view can render it (see
        :meth:`min_passes_for`).
    heading, lede:
        The gallery section's heading and one-paragraph explanation. The lede is
        emitted as trusted inline markup (module constants, never user input).
    alt_template:
        ``str.format`` template for an image tile's ``alt`` text, given
        ``label``. Unused for ``"page"`` tiles.
    """

    name: str
    suffix: str
    kind: str
    min_passes: int
    heading: str
    lede: str
    alt_template: str = ""

    def min_passes_for(self, frames: int) -> int:
        """Passes a site needs to qualify for this view at ``frames``.

        Only the change view's requirement moves with ``--featured-frames`` (it
        composites exactly that many passes); the timescan needs its statistical
        minimum whatever ``frames`` says, and a swipe is always two.
        """
        return max(self.min_passes, frames) if self.name == "change" else self.min_passes


#: The featured-gallery views, keyed by their ``--featured-view`` name. The
#: change view is the default and the original behaviour; the other two are the
#: same marquee selection rendered by the other two ``viz`` comparators.
FEATURED_VIEWS: dict[str, FeaturedView] = {
    "change": FeaturedView(
        name="change",
        suffix=".png",
        kind="image",
        min_passes=2,
        heading="What SAR change looks like",
        lede=(
            "Each image composites repeat passes of one site onto a shared grid: "
            "ground that stayed put reads gray, and anything that appeared or "
            "vanished between passes is tinted by when it happened. Rendered ahead "
            "of time from the open catalog \N{EM DASH} no account, no download."
        ),
        alt_template="SAR change composite of {label}",
    ),
    "timescan": FeaturedView(
        name="timescan",
        suffix=".png",
        kind="image",
        min_passes=3,
        heading="What a whole time series looks like",
        lede=(
            "Each image collapses <em>every</em> pass of one site into one picture "
            "of temporal statistics on a shared grid: average backscatter in red, "
            "the peak in green, and how much each pixel varied across the series in "
            "blue. Stable terrain reads gray or yellow; anything that came and went "
            "\N{EM DASH} ships through a berth, vehicles in a lot, a field flooding "
            "\N{EM DASH} glows blue or cyan."
        ),
        alt_template="SAR timescan composite of {label}",
    ),
    "swipe": FeaturedView(
        name="swipe",
        suffix=".html",
        kind="page",
        min_passes=2,
        heading="Sweep between two passes",
        lede=(
            "Each tile opens a co-registered before/after map of one site with a "
            "draggable divider: the earlier pass fills one side, the later one the "
            "other, and sweeping the seam wipes one over the identical ground. It "
            "is the most direct way to <em>feel</em> what moved between two dates. "
            "Every map is a self-contained page, rendered ahead of time from the "
            "open catalog."
        ),
    ),
}

#: The view built when none is named -- the original featured gallery.
DEFAULT_FEATURED_VIEW = "change"

#: Ordered view names, for the CLI's ``--featured-view`` choice.
FEATURED_VIEW_NAMES = tuple(FEATURED_VIEWS)


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
    """One rendered tile in the landing page's featured gallery.

    ``kind`` mirrors the :class:`FeaturedView` that produced it: an ``"image"``
    tile *is* the picture, a ``"page"`` tile is a link card onto an interactive
    HTML artifact that has no still to show.

    ``narration`` / ``narration_href`` carry an optional precomputed VLM reading
    of the site's change (Mode A, ``umbra showcase --narrate``): the plain-language
    ``summary`` shown under the tile and the relative path to the full JSON
    sidecar it was cut from. Both are ``None`` on a build without ``--narrate`` or
    a model key, so a gallery without narration is byte-identical to before.
    """

    href: str
    label: str
    caption: str
    kind: str = "image"
    alt: str = ""
    narration: str | None = None
    narration_href: str | None = None


#: Renders one site's featured artifact to a path. Injectable so the whole
#: assembler stays offline-testable; the default calls into ``umbra_py.viz``.
FeaturedRenderer = Callable[[list[UmbraItem], Path], None]

#: Narrates one site's change, returning the narration document (the
#: ``ChangeNarration.to_dict()`` shape) or ``None`` to skip it. Injectable, like
#: :data:`FeaturedRenderer`, so the assembler stays offline-testable and never
#: calls a model in tests; the default (:func:`_default_featured_narrator`)
#: reads the same passes the composite shows and calls :func:`umbra_py.narrate`.
FeaturedNarrator = Callable[[list[UmbraItem]], "dict[str, Any] | None"]


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
    rank_by: str = "passes",
    active_since: DateLike = None,
    active_before: DateLike = None,
    first_since: DateLike = None,
    first_before: DateLike = None,
    max_revisit_days: float | None = None,
    median_revisit_days: float | None = None,
    min_span_days: float | None = None,
    max_span_days: float | None = None,
) -> list[FeaturedSite]:
    """Choose the most repeat-imaged sites in ``items`` for the featured gallery.

    A change composite needs two or more dated passes over the *same* ground,
    and Umbra files every pass of a site under one task directory -- so the
    tasks with the most acquisitions in the candidate pool are exactly the ones
    worth precomputing. Sites are ranked by ``rank_by`` (descending) and then by
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
        composite can use), measured on the same depth ``rank_by`` ranks by: under
        ``"comparable"`` a site qualifies on its *analysable* depth (its
        ``comparable_passes`` largest single-polarization dated subset), so a site
        whose raw count clears the floor but whose differenceable series does not is
        not admitted. Under ``"passes"`` it is the raw dated pass count, unchanged.
    rank_by:
        One of :data:`umbra_py.coverage.SITE_RANKINGS`. The *depth* rankings are
        ``"passes"`` (the default, and what the featured gallery uses -- raw pass
        count) and ``"comparable"`` (each site's *analysable* depth, the largest
        single-polarization dated subset a change verb can difference, so a
        broad-but-mixed site cannot outrank -- or, with ``min_passes``, qualify past
        -- a deeper single-polarization series). The *temporal* rankings are
        ``"recency"`` (each site's newest dated pass first), ``"span"`` (each site's
        observation baseline first) and ``"cadence"`` (each site's *typical* revisit
        gap, tightest first -- the median gap, not the worst one), ordering by the
        whole-site ``last`` / ``span_days`` / ``median_revisit_days`` figures, ties
        broken by raw depth then task. The chosen key is applied before the ``count``
        truncation, so the ordering is over the whole qualifying pool rather than a
        raw-ranked prefix of it (a recently-active, long-baseline or tightly-revisited
        site outside the raw top-``count`` still surfaces).
    active_since:
        Keep only sites still being imaged *on or after* this date -- a recency
        filter on the site's **newest** dated pass, so a deeply-imaged series that
        stopped years ago is dropped while an actively-revisited one is kept. It
        accepts anything :func:`umbra_py.dates.parse_date_bound` does (an ISO date,
        a bare year/month, or a relative expression like ``"6 months ago"``). It is
        orthogonal to ``rank_by`` and ``min_passes`` -- it gates on the whole site's
        latest pass (the ``last`` a coverage summary reports), independent of which
        depth the ranking measures -- and to ``start`` / ``end``: those bound which
        *passes* enter the pool (truncating every series to a window), whereas this
        selects whole sites by recency and keeps each surviving site's *full*
        history. ``None`` (the default) applies no recency filter.
    active_before:
        The complement of ``active_since``: keep only sites whose **newest** dated
        pass is *on or before* this date, so a series that stopped imaging (a
        dormant site) is kept and one still being revisited is dropped. Set with
        ``active_since`` it selects sites whose latest pass falls *within* a window
        (``active_since <= last <= active_before``). It accepts the same grammar
        ``active_since`` does, but a span expression snaps to its *last* day (a bare
        year/month covers the whole named period), so ``active_before="2024"`` means
        "last imaged on or before 2024-12-31" -- symmetric with the ``end`` bound.
        Like ``active_since`` it gates on the whole site's latest pass, independent
        of ``rank_by`` / ``min_passes`` and of ``start`` / ``end``. ``None`` (the
        default) applies no upper recency bound.
    first_since:
        The onset (first-seen) twin of ``active_since``: keep only sites whose
        **earliest** dated pass is *on or after* this date -- a **newly-appeared**
        series, one that entered the archive recently -- where ``active_since`` gates
        the newest pass (is it still being imaged). Same grammar as ``active_since``.
        It gates on the whole site's earliest pass (the ``first`` a coverage summary
        reports), independent of ``rank_by`` / ``min_passes`` and distinct from
        ``start`` / ``end``. ``None`` (the default) applies no onset filter.
    first_before:
        The complement of ``first_since`` (and the onset twin of ``active_before``):
        keep only sites whose **earliest** dated pass is *on or before* this date -- a
        **long-established** series, watched since before then. Set with ``first_since``
        it bounds the onset to a window (``first_since <= first <= first_before``),
        exactly as ``active_since`` / ``active_before`` bound the newest pass. A span
        expression snaps to its *last* day (``first_before="2024"`` is "first imaged on
        or before 2024-12-31"), symmetric with ``active_before`` / ``end``. ``None``
        (the default) applies no lower onset bound.
    max_revisit_days:
        Keep only sites revisited *at least this often* -- a cadence filter on each
        site's **worst-case** revisit gap, so a series with any stretch longer than
        ``max_revisit_days`` days between consecutive passes is dropped and a
        reliably-imaged one kept. It gates the same depth ``rank_by`` measures
        (:func:`umbra_py.coverage._passes_cadence`): under ``"comparable"`` the
        *analysable* series' worst gap (``comparable_max_revisit_days``), so a site
        whose raw cadence looks tight only because an off-polarization pass fills a
        gap no change verb can use is not admitted. It is orthogonal to
        ``active_since`` / ``active_before`` and to ``start`` / ``end``. A site with
        fewer than two passes in the gated series has no measurable cadence and is
        dropped. ``None`` (the default) applies no cadence filter.
    median_revisit_days:
        The *typical*-cadence twin of ``max_revisit_days`` -- keep only sites whose
        **median** revisit gap is at most this many days, so a site *usually* imaged
        often is kept even if a single stretch runs long, where ``max_revisit_days``
        drops it the moment any gap exceeds the bound. It gates the same depth
        ``rank_by`` measures (:func:`umbra_py.coverage._passes_median_revisit`): under
        ``"comparable"`` the *analysable* series' typical gap
        (``comparable_median_revisit_days``). It is orthogonal to ``max_revisit_days``
        (typical gap vs worst gap), to the recency bounds and to ``start`` / ``end``.
        A site with fewer than two passes in the gated series is dropped. ``None``
        (the default) applies no typical-cadence filter.
    min_span_days:
        Keep only sites imaged over *at least this long* -- a baseline filter on each
        site's observation **span** (whole days from its first dated pass to its
        last), so a series confined to a short window is dropped and a long-baseline
        one kept. A different axis from ``max_revisit_days``: cadence is the worst
        *gap* (how reliably a site is watched), span is the total *baseline* (how
        long it has been watched at all), the window a slow change needs to be
        visible in. It gates the same series ``rank_by`` measures
        (:func:`umbra_py.coverage._passes_span`): under ``"comparable"`` the
        *analysable* series' span (``comparable_span_days``), so off-polarization
        passes bracketing the range cannot inflate the baseline past the series a
        change verb can difference. It is orthogonal to ``active_since`` /
        ``active_before``, to ``max_revisit_days`` and to ``start`` / ``end``. A site
        with fewer than two passes in the gated series has no measurable span and is
        dropped. ``None`` (the default) applies no span filter.
    max_span_days:
        The upper twin of ``min_span_days`` -- keep only sites imaged over *at most
        this long*, so a short-window series is kept and a long-baseline one dropped.
        It selects the complement of ``min_span_days`` (the *short-lived* series, now
        over, rather than the long-baseline one a slow change needs), and set with
        ``min_span_days`` the two bound each site's baseline to a window
        (``min_span_days <= span <= max_span_days``), as ``active_since`` /
        ``active_before`` bound the newest pass to one. It gates the same series
        ``rank_by`` measures (:func:`umbra_py.coverage._passes_max_span`): under
        ``"comparable"`` the *analysable* series' span, so off-polarization passes
        bracketing the range cannot make a baseline read longer than the differenceable
        series. Like ``min_span_days`` it drops a site with fewer than two passes in
        the gated series, so the two compose as a clean window. ``None`` (the default)
        applies no span ceiling.

    Returns the sites best-first, each carrying its passes oldest-first.
    Deterministic and dependency-free -- it calls no renderer and no model.
    """
    if count <= 0:
        return []
    since = _coerce_date(active_since)
    # ``active_before`` snaps a span expression to its last day (``is_end``), so a
    # bare year/month bounds the whole named period -- symmetric with ``end``.
    before = _coerce_date(active_before, is_end=True)
    # The onset (first-seen) bounds gate the *earliest* pass, snapping the same way:
    # ``first_since`` to a span's first day, ``first_before`` to its last (so a bare
    # year/month bounds the whole named period), symmetric with the recency pair.
    first_since_date = _coerce_date(first_since)
    first_before_date = _coerce_date(first_before, is_end=True)
    # Single-sourced with the discovery layer: the same key builder and the same
    # comparable-group definition, so the featured gallery, ``umbra sites`` and the
    # index ranker cannot disagree about what "most repeat-imaged" means under
    # either ranking (lazy import -- the default ``"passes"`` path needs neither).
    from .coverage import (  # noqa: PLC0415
        _check_max_revisit,
        _check_max_span,
        _check_median_revisit,
        _check_min_span,
        _check_ranking,
        _largest_comparable_group,
        _min_passes_depth,
        _passes_cadence,
        _passes_max_span,
        _passes_median_revisit,
        _passes_span,
        _rank_sort_key,
        _temporal_rank_figures,
    )

    _check_ranking(rank_by)
    _check_max_revisit(max_revisit_days)
    _check_median_revisit(median_revisit_days)
    _check_min_span(min_span_days)
    _check_max_span(max_span_days)
    by_task: dict[str, list[UmbraItem]] = {}
    for item in items:
        if item.task and item.datetime is not None:
            by_task.setdefault(item.task, []).append(item)

    # Rank and qualify a site on the same depth (``_min_passes_depth`` /
    # ``_rank_sort_key``): under ``"comparable"`` ``min_passes`` gates on analysable
    # depth, so a site whose raw count clears the floor but whose differenceable
    # series does not is dropped rather than admitted at the bottom of the list. The
    # comparable group is only read under the comparable ranking -- the default
    # passes path (the featured gallery) neither ranks nor qualifies on it. Every
    # pass here has a datetime (filtered above); the ``or datetime.min`` fallback is
    # unreachable but keeps the sort key typed, as in viz.py.
    ranked: list[tuple[FeaturedSite, int]] = []
    for task, passes in by_task.items():
        ordered = sorted(passes, key=lambda i: i.datetime or datetime.min)
        # Recency gate: select a site by its newest dated pass. ``ordered`` is
        # oldest-first and every member is dated (filtered above), so the last pass
        # is the latest -- the ``last`` a summary reports. ``active_since`` drops a
        # site whose latest predates the lower cutoff; ``active_before`` drops one
        # whose latest is after the upper cutoff (so the two together keep sites
        # whose latest pass falls within the window). The ``or datetime.min`` keeps
        # the read typed, exactly as the sort key does.
        newest = (ordered[-1].datetime or datetime.min).date()
        if since is not None and newest < since:
            continue
        if before is not None and newest > before:
            continue
        # Onset gate: select a site by its *earliest* dated pass -- the ``first`` a
        # summary reports. ``ordered`` is oldest-first and every member is dated
        # (filtered above), so the first pass is the earliest. ``first_since`` drops a
        # site whose earliest predates the lower cutoff (keeping newly-appeared series);
        # ``first_before`` drops one whose earliest is after the upper cutoff (keeping
        # long-established ones), so the two together keep sites whose first pass falls
        # within the window. Independent of the recency gate above: a site can be new
        # *and* still active, or old *and* dormant, so the two axes select orthogonally.
        earliest = (ordered[0].datetime or datetime.min).date()
        if first_since_date is not None and earliest < first_since_date:
            continue
        if first_before_date is not None and earliest > first_before_date:
            continue
        # Cadence gate: keep a site only if the series ``rank_by`` measures is
        # revisited at least this often (its worst gap is at most the bound). Gated
        # on the same items :func:`umbra_py.coverage.site_coverage` summarises, so
        # this and the reported ``max_revisit_days`` cannot disagree -- and gated on
        # the *analysable* subset under ``"comparable"`` (the cadence twin of
        # ``min_passes`` measuring comparable depth). A site with no measurable
        # cadence in that series is dropped.
        if max_revisit_days is not None and not _passes_cadence(
            ordered, rank_by=rank_by, max_revisit_days=max_revisit_days
        ):
            continue
        # Typical-cadence gate: keep a site only if the series ``rank_by`` measures is
        # *usually* revisited at least this often (its median gap is at most the
        # bound). The complement of the worst-case gate above -- it tolerates a single
        # long outage where ``max_revisit`` does not. Gated on the same items and the
        # same ``_passes_median_revisit`` the index path uses (byte-identical), and on
        # the *analysable* subset under ``"comparable"``. A site with no measurable
        # cadence is dropped.
        if median_revisit_days is not None and not _passes_median_revisit(
            ordered, rank_by=rank_by, median_revisit_days=median_revisit_days
        ):
            continue
        # Span gate: keep a site only if the series ``rank_by`` measures covers a
        # long enough baseline (its first-to-last span is at least the bound). Gated
        # on the same items the summary reduces, so this and the reported
        # ``span_days`` cannot disagree -- and on the *analysable* subset under
        # ``"comparable"`` (the baseline twin of ``max_revisit`` gating comparable
        # cadence). A site with no measurable span in that series is dropped.
        if min_span_days is not None and not _passes_span(
            ordered, rank_by=rank_by, min_span_days=min_span_days
        ):
            continue
        # Span ceiling: the upper twin of the ``min_span`` floor, keeping only sites
        # whose baseline is at most the bound (a short-lived series). Gated on the same
        # items and the same ``_passes_max_span`` the index path uses (byte-identical),
        # and on the *analysable* subset under ``"comparable"``. Set with the floor
        # above the two bound the baseline to a window; like the floor, a site with no
        # measurable span is dropped, so the window admits only a confirmed baseline.
        if max_span_days is not None and not _passes_max_span(
            ordered, rank_by=rank_by, max_span_days=max_span_days
        ):
            continue
        comparable = len(_largest_comparable_group(ordered)) if rank_by == "comparable" else 0
        depth = _min_passes_depth(
            comparable_passes=comparable, passes=len(ordered), rank_by=rank_by
        )
        if depth >= min_passes:
            ranked.append((FeaturedSite(task=task, items=ordered), comparable))

    # The temporal rankings ("recency" / "span" / "cadence") order by whole-site
    # figures reduced from the site's own passes -- computed here by the same
    # `_temporal_rank_figures` the index path reads back off a `SiteCoverage`, so the
    # two paths cannot disagree. The depth rankings ignore them.
    def _sort_key(pair: tuple[FeaturedSite, int]) -> tuple[object, ...]:
        site, comparable = pair
        last, span_days, median_revisit_days = _temporal_rank_figures(site.items)
        return _rank_sort_key(
            comparable_passes=comparable,
            passes=len(site.items),
            task=site.task,
            rank_by=rank_by,
            last=last,
            span_days=span_days,
            median_revisit_days=median_revisit_days,
        )

    ranked.sort(key=_sort_key)
    return [site for site, _ in ranked[:count]]


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
    featured_view: str = DEFAULT_FEATURED_VIEW,
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
        Precomputed artifacts to show above the cards. Empty (the default) drops
        the whole gallery section, so a metadata-only showcase is byte-identical
        to one built before this option existed.
    featured_view:
        Which :data:`FEATURED_VIEWS` entry produced ``featured`` — it selects the
        section's heading and lede. Ignored when ``featured`` is empty.
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
        featured=_featured_section(featured, featured_view),
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


def _featured_section(
    featured: Sequence[FeaturedArtifact], view: str = DEFAULT_FEATURED_VIEW
) -> str:
    """Render the precomputed-artifact gallery (empty string when there is
    nothing to show, so the page has no dangling heading).

    Each tile points at its own file inside ``featured/`` -- deliberately not at
    a render endpoint, so the section keeps working on a plain static host. The
    heading and lede come from the :class:`FeaturedView` that produced the
    artifacts, and each tile takes the shape that view's output justifies.
    """
    if not featured:
        return ""
    spec = FEATURED_VIEWS.get(view, FEATURED_VIEWS[DEFAULT_FEATURED_VIEW])
    figures = "\n".join(_featured_tile(art) for art in featured)
    return (
        '    <section class="featured">\n'
        f"      <h2>{escape(spec.heading)}</h2>\n"
        f'      <p class="lede">{spec.lede}</p>\n'
        '      <div class="shots">\n'
        f"{figures}\n"
        "      </div>\n"
        "    </section>"
    )


def _featured_tile(art: FeaturedArtifact) -> str:
    """One gallery tile, shaped by what its renderer wrote.

    An ``"image"`` artifact is shown inline and links to itself at full size. A
    ``"page"`` artifact (the swipe map) has no still to preview, so its tile is a
    link card in the same frame -- the caption below is identical either way, so
    the two shapes read as one gallery.
    """
    href = escape(art.href, quote=True)
    caption = (
        f"          <figcaption><strong>{escape(art.label)}</strong>"
        f"<span>{escape(art.caption)}</span>"
        f"{_narration_block(art)}</figcaption>\n"
    )
    if art.kind == "page":
        body = (
            f'          <a class="glyph" href="{href}">'
            f'<span aria-hidden="true">\N{SQUARE WITH LEFT HALF BLACK}</span>'
            f'<span class="open">Open the swipe map</span></a>\n'
        )
        return f'        <figure class="shot shot--page">\n{body}{caption}        </figure>'
    alt = escape(art.alt or art.label, quote=True)
    body = f'          <a href="{href}"><img src="{href}" alt="{alt}" loading="lazy"/></a>\n'
    return f'        <figure class="shot">\n{body}{caption}        </figure>'


def _narration_block(art: FeaturedArtifact) -> str:
    """The precomputed AI reading shown under a tile, or "" when there is none.

    Labelled as an AI interpretation and linked to its JSON sidecar, so a visitor
    sees a plain-language "what changed here" without a live model call — and
    knows it is a model's reading of radar, not ground truth (design principle:
    the determinism boundary is visible, not just honoured)."""
    if not art.narration:
        return ""
    reading = f'<span class="reading">{escape(art.narration)}</span>'
    if art.narration_href:
        link = escape(art.narration_href, quote=True)
        reading += (
            f'<a class="reading-more" href="{link}">Full reading &amp; the numbers it cites</a>'
        )
    return f'<span class="narration"><span class="tag">AI reading</span>{reading}</span>'


def featured_caption(site: FeaturedSite, frames: int, *, view: str = DEFAULT_FEATURED_VIEW) -> str:
    """One line of honest provenance under a featured tile: how many passes went
    in, over what dates, and what the colours (or the interaction) mean.

    The semantics come straight from the ``viz`` renderer behind each view --
    :func:`umbra_py.viz.change_composite` (two dates -> green/magenta, three ->
    temporal RGB), :func:`umbra_py.viz.timescan_composite` (mean/max/std as RGB
    over the *whole* series) and :func:`umbra_py.viz.swipe_map` (two passes
    behind a divider); keep them in step.
    """
    if view == "timescan":
        # The timescan summarises every pass, not a selection of `frames`.
        used = len(site.items)
        detail = (
            "red = average backscatter, green = peak, blue = variability; "
            "stable ground reads gray, anything that came and went glows blue/cyan"
        )
    elif view == "swipe":
        used = 2
        detail = "drag the divider to sweep the later pass over the earlier one"
    else:
        used = frames
        detail = (
            "green = new or brighter backscatter, magenta = gone or dimmer"
            if frames == 2
            else "earliest = red, middle = green, latest = blue"
        )
    span = site.date_range
    passes = f"{used} passes" if used != 1 else "1 pass"
    return f"{passes}, {span} \N{EM DASH} {detail}." if span else f"{passes} \N{EM DASH} {detail}."


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
    featured_view: str = DEFAULT_FEATURED_VIEW,
    featured_renderer: FeaturedRenderer | None = None,
    featured_narrator: FeaturedNarrator | None = None,
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
    * ``featured/<slug>.png`` (or ``.html`` for the swipe view) -- one
      precomputed artifact per entry in ``featured_sites`` (none by default).
    * ``index.html`` -- always (the landing page, with cards for whichever of the
      above were written).

    Parameters
    ----------
    items:
        Acquisitions for the explorer. ``None`` or empty skips ``explore.html``
        and its card.
    featured_sites:
        Sites (from :func:`select_featured_sites`) to precompute an artifact
        for. Empty (the default) leaves the page and the output directory
        exactly as they were before this option existed.
    featured_frames:
        Passes composited into each featured image -- 2 (green/magenta) or 3
        (temporal RGB), per :func:`umbra_py.viz.change_composite`. Only the
        ``"change"`` view uses it: a timescan summarises the whole series and a
        swipe is always two passes.
    featured_view:
        Which of :data:`FEATURED_VIEWS` to render -- ``"change"`` (the default
        two/three-date composite), ``"timescan"`` (the whole series collapsed to
        temporal statistics) or ``"swipe"`` (an interactive before/after page).
        It selects the default renderer, the artifact extension and the gallery
        section's copy.
    featured_renderer:
        ``(items, dest) -> None``, called once per featured site. Defaults to
        the ``viz`` function behind ``featured_view``
        (:func:`umbra_py.viz.save_change_composite` over the frames
        :func:`umbra_py.viz.select_change_frames` picks,
        :func:`umbra_py.viz.save_timescan_composite`, or
        :func:`umbra_py.viz.save_swipe_map`), which needs the ``viz`` extra and
        streams each scene's overview. A site whose render fails is warned about
        and dropped, never fatal: one unreadable asset must not cost the whole
        showcase.
    featured_narrator:
        ``(items) -> dict | None``, called once per rendered featured site to
        bake a precomputed VLM narration of its change into
        ``featured/<slug>.narration.json`` and a summary line under the tile
        (Mode A). ``None`` (the default) bakes nothing, so the output is
        byte-identical to a build without it. The production narrator
        (``umbra showcase --narrate``) reads the same passes the ``change``
        composite shows and needs the ``ai`` + ``viz`` extras plus a model key;
        it is injected here so tests never call a model. A narrator that returns
        ``None`` or raises leaves the tile untouched — the model call is the one
        build step that can fail for reasons unrelated to the data.
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

    Deterministic and offline in its default form: it only copies a file and
    calls the existing ``save_viewer`` / ``save_demo`` writers, so it needs no
    network and no ``viz`` extra. The two injectable seams are where that changes
    by choice — the production ``featured_renderer`` streams each scene's overview
    (``viz``), and a ``featured_narrator`` calls a model (``ai``) — and both are
    ``None`` by default.
    """
    if unified and pmtiles_path is None:
        raise ValueError("unified=True needs a pmtiles_path (the archive is the data source)")
    if featured_view not in FEATURED_VIEWS:
        raise ValueError(
            f"unknown featured_view {featured_view!r}; expected one of "
            f"{', '.join(FEATURED_VIEW_NAMES)}"
        )

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
        view=featured_view,
        renderer=featured_renderer,
        narrator=featured_narrator,
    )

    index = dest / "index.html"
    index.write_text(
        build_showcase(
            map_href=map_href,
            explore_href=explore_href,
            unified=unified,
            item_count=len(item_list) if item_list is not None else None,
            featured=featured,
            featured_view=featured_view,
            **showcase_kwargs,
        )
    )
    return index


def _render_featured(
    dest: Path,
    sites: Sequence[FeaturedSite],
    *,
    frames: int,
    view: str = DEFAULT_FEATURED_VIEW,
    renderer: FeaturedRenderer | None,
    narrator: FeaturedNarrator | None = None,
) -> list[FeaturedArtifact]:
    """Render one artifact per site into ``dest/featured/`` and return the ones
    that actually landed (a failed render is skipped, not fatal).

    When ``narrator`` is given, each site that rendered is also narrated and its
    reading written to ``featured/<slug>.narration.json`` (Mode A). Narration is
    strictly additive: a narrator that returns ``None`` or raises leaves the tile
    exactly as it would be without one, so one model hiccup costs a caption line,
    never the gallery."""
    if not sites:
        return []
    spec = FEATURED_VIEWS[view]
    render = renderer if renderer is not None else _default_featured_renderer(frames, view=view)
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

        out_path = out_dir / f"{stem}{spec.suffix}"
        try:
            render(site.items, out_path)
        except Exception as exc:  # noqa: BLE001 - one bad scene must not fail the build
            warnings.warn(
                f"Skipping featured site {site.task!r}: {exc}", RuntimeWarning, stacklevel=2
            )
            continue
        if not out_path.exists():  # a renderer that silently wrote nothing
            continue
        label = site.label
        narration, narration_href = _narrate_featured(out_dir, stem, site, narrator)
        artifacts.append(
            FeaturedArtifact(
                href=f"{FEATURED_DIR}/{out_path.name}",
                label=label,
                caption=featured_caption(site, min(frames, len(site.items)), view=view),
                kind=spec.kind,
                alt=spec.alt_template.format(label=label) if spec.alt_template else "",
                narration=narration,
                narration_href=narration_href,
            )
        )
    return artifacts


def _narrate_featured(
    out_dir: Path,
    stem: str,
    site: FeaturedSite,
    narrator: FeaturedNarrator | None,
) -> tuple[str | None, str | None]:
    """Narrate one featured site and write its JSON sidecar (Mode A).

    Returns ``(summary, href)`` — the plain-language line to show under the tile
    and the relative path to the full narration JSON — or ``(None, None)`` when
    there is no narrator, the narrator declined (``None``), or it raised. A
    failure here is warned about and swallowed: a model call is the one part of
    the build that can fail for reasons that have nothing to do with the data
    (no key, spend cap, a timeout), and none of those should cost the picture
    that already rendered."""
    if narrator is None:
        return None, None
    try:
        document = narrator(site.items)
    except Exception as exc:  # noqa: BLE001 - a model hiccup must not fail the build
        warnings.warn(
            f"No narration for featured site {site.task!r}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None, None
    if not document:
        return None, None
    narration_path = out_dir / f"{stem}.narration.json"
    narration_path.write_text(json.dumps(document, indent=2))
    summary = document.get("summary")
    return (summary or None), f"{FEATURED_DIR}/{narration_path.name}"


def _default_featured_renderer(
    frames: int, asset: str = "GEC", *, view: str = DEFAULT_FEATURED_VIEW
) -> FeaturedRenderer:
    """The production renderer for ``view``. Needs the ``viz`` extra, and streams
    only a downsampled overview of each scene.

    * ``change`` -- co-register the evenly-spaced ``frames`` of a site and write
      the change composite.
    * ``timescan`` -- collapse the site's *whole* series into one temporal
      statistics image (mean/max/std as RGB).
    * ``swipe`` -- write a self-contained before/after page over the site's first
      and last comparable passes.
    """

    def render_change(items: list[UmbraItem], dest: Path) -> None:
        from .viz import save_change_composite, select_change_frames  # noqa: PLC0415

        save_change_composite(select_change_frames(items, frames=frames), dest, asset=asset)

    def render_timescan(items: list[UmbraItem], dest: Path) -> None:
        from .viz import save_timescan_composite  # noqa: PLC0415

        save_timescan_composite(items, dest, asset=asset)

    def render_swipe(items: list[UmbraItem], dest: Path) -> None:
        from .viz import save_swipe_map, select_change_frames  # noqa: PLC0415

        # The same two-frame selection the change view uses (largest
        # single-polarization group, earliest and latest), so the two views tell
        # the same story about a site.
        before, after = select_change_frames(items, frames=2)
        save_swipe_map(before, after, dest, asset=asset)

    return {
        "change": render_change,
        "timescan": render_timescan,
        "swipe": render_swipe,
    }[view]


def _default_featured_narrator(
    frames: int,
    asset: str = "GEC",
    *,
    view: str = DEFAULT_FEATURED_VIEW,
    model: str | None = None,
) -> FeaturedNarrator | None:
    """The production narrator for ``view``, or ``None`` when the view has no
    two/three-pass change for a model to read.

    It narrates **the same passes the composite shows** — ``select_change_frames``
    picks them for both the ``change`` render and this reading — so the picture
    and the plain-language summary describe one pair, not two. Only the ``change``
    view qualifies: a ``timescan`` collapses the whole series (no single pair) and
    a ``swipe`` is an interactive page rather than a still, so neither gets a
    baked reading here. Needs the ``ai`` + ``viz`` extras and a model key at call
    time; a keyless or failing call is swallowed per site by
    :func:`_narrate_featured`."""
    if view != "change":
        return None

    def narrate_site(items: list[UmbraItem]) -> dict[str, Any] | None:
        from .narrate import narrate  # noqa: PLC0415
        from .viz import select_change_frames  # noqa: PLC0415

        picked = select_change_frames(items, frames=frames)
        return narrate(picked, asset=asset, model=model).to_dict()

    return narrate_site


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
    .shot--page .glyph {
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; gap: .5rem; aspect-ratio: 1;
      text-decoration: none; color: inherit;
      background: linear-gradient(135deg, var(--border), var(--panel));
      border-bottom: 1px solid var(--border); font-size: 2.5rem;
      transition: background .12s ease;
    }
    .shot--page .glyph .open { font-size: .9rem; color: var(--accent); }
    .shot--page .glyph:hover { background: linear-gradient(135deg, var(--panel), var(--border)); }
    .shot figcaption { padding: .75rem 1rem 1rem; font-size: .85rem; }
    .shot figcaption strong { display: block; margin-bottom: .15rem; }
    .shot figcaption > span { color: var(--muted); }
    .narration {
      display: block; margin-top: .6rem; padding-top: .6rem;
      border-top: 1px solid var(--border); color: var(--fg);
    }
    .narration .tag {
      display: inline-block; margin-bottom: .3rem; padding: .05em .5em;
      border-radius: 999px; background: rgba(124,156,255,.15);
      color: var(--accent); font-size: .72rem; font-weight: 600;
      letter-spacing: .02em; text-transform: uppercase;
    }
    .narration .reading { display: block; color: var(--fg); }
    .narration .reading-more {
      display: inline-block; margin-top: .35rem; color: var(--accent);
      font-size: .8rem; text-decoration: none;
    }
    .narration .reading-more:hover { text-decoration: underline; }
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
