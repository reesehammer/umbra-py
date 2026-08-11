"""Rank the archive's most repeat-imaged sites and summarise each one's coverage.

Every analysis verb in this library -- ``umbra change``, ``umbra timescan``,
``umbra stack``, ``umbra change --narrate``, the ``stack_stats`` cube -- begins
with a question it does not answer: *which* site has a time series worth looking
at? Umbra files every pass of an area under one task directory, so a site's
coverage is just how many dated passes share its task; the sites with the most
are exactly the ones where change detection has something to measure. The static
showcase already picks them (:func:`umbra_py.showcase.select_featured_sites`) to
precompute its featured gallery, but that ranking was invisible to anyone not
building a showcase.

This module turns that ranking into a first-class discovery answer. It reuses
the showcase's selector for the grouping (so the two surfaces cannot disagree
about what "most repeat-imaged" means) and adds a per-site *coverage summary* --
pass count, date span, revisit cadence, footprint, products and the pass URLs
ready to hand straight to ``umbra change`` / ``umbra stack``. It is pure and
dependency-free (stdlib + :class:`~umbra_py.models.UmbraItem`), so it runs on a
core install and is exercised entirely offline; :func:`umbra_py.cli.sites`
gathers the candidate pool and prints what this returns.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import TYPE_CHECKING

from .models import BBox, UmbraItem

if TYPE_CHECKING:
    from .catalog import DateLike

#: The orderings :func:`rank_site_coverage` (and every discovery surface that
#: forwards to it) can rank sites by. The first two rank by *depth*; the last two
#: by a *temporal* figure the discovery answer already reports and can already
#: filter on -- so the moat now ranks on every axis it filters on, not only on
#: depth.
#:
#: ``"passes"`` is the historical default -- raw pass count, most-imaged first --
#: which is what the static showcase's featured gallery wants (more acquisitions
#: to precompute, whatever their polarization mix). ``"comparable"`` ranks by
#: *analysable* depth instead: the ``comparable_passes`` figure, i.e. the largest
#: single-polarization dated subset an analysis verb (``change`` / ``timescan`` /
#: ``stack``, ``stack_stats``, ``change --narrate``) can actually difference. The
#: two disagree exactly when a raw count overstates what is analysable -- a site
#: whose passes span several polarizations, or carry undated passes -- so a
#: shallow-but-broad site can outrank a deep single-polarization series under
#: ``"passes"`` yet fall behind it under ``"comparable"``. The report has
#: distinguished the two figures since the comparable-figure workstream; this lets
#: the *ranking* distinguish them too.
#:
#: ``"recency"`` orders by each site's **newest** dated pass (the ``last`` a
#: summary reports), most-recently-active first -- the site a monitoring or
#: tasking user (STRATEGY.md §1's funnel) would reach for, which a depth ranking
#: buries under a deeper series that stopped years ago. ``"span"`` orders by each
#: site's observation **baseline** (the ``span_days`` a summary reports),
#: longest-watched first -- the window a *slow* change (subsidence, construction,
#: deforestation) needs to be visible in. Both order by a whole-site figure
#: independent of the polarization grouping (recency and baseline are facts about
#: the site's activity, not about one differenceable subset), with ties broken by
#: raw depth then task name for full determinism; ``min_passes`` still qualifies a
#: site on the depth ``rank_by`` would measure under ``"passes"`` (raw pass count).
SITE_RANKINGS: tuple[str, ...] = ("passes", "comparable", "recency", "span")


def _check_ranking(rank_by: str) -> None:
    """Reject an unknown ``rank_by``, naming the accepted set (shared by every
    surface so they cannot disagree about what a ranking is)."""
    if rank_by not in SITE_RANKINGS:
        raise ValueError(f"rank_by must be one of {SITE_RANKINGS}, got {rank_by!r}")


def _rank_sort_key(
    *,
    comparable_passes: int,
    passes: int,
    task: str,
    rank_by: str,
    last: date | None = None,
    span_days: int | None = None,
) -> tuple[object, ...]:
    """The deterministic sort key for one site under ``rank_by``.

    ``"passes"`` orders by raw pass count then task name (the historical key
    :func:`umbra_py.showcase.select_featured_sites` has always used). ``"comparable"``
    orders by analysable depth first, breaking ties by raw pass count (so among
    equally-analysable sites the one with more total context ranks higher) and then
    by task name for full determinism. ``"recency"`` orders by the site's newest
    dated pass (``last``), most recent first, and ``"span"`` by its observation
    baseline (``span_days``), longest first; both break ties by raw pass count then
    task name. All four are ascending over negated figures, so a plain ``sort`` puts
    the best site first.

    ``last`` and ``span_days`` are the whole-site figures :class:`SiteCoverage`
    reports; a caller passes them only for the temporal rankings (the depth rankings
    ignore them). ``last`` is always present for a ranking candidate (it has at least
    one dated pass), so the ``date.min`` fallback is unreachable and only keeps the
    key typed. ``span_days`` is ``None`` for a site with fewer than two dated passes;
    such a site has no measurable baseline, so it sorts *after* every measured-span
    site (treated as ``-1`` day) rather than erroring -- a ``"span"`` ranking stays a
    total order. Single-sourced so :func:`select_featured_sites` (ranking
    ``FeaturedSite`` before summarising) and
    :meth:`umbra_py.index.CatalogIndex.rank_sites` (ranking summarised
    :class:`SiteCoverage` records) cannot pick a different order.
    """
    if rank_by == "comparable":
        return (-comparable_passes, -passes, task)
    if rank_by == "recency":
        return (-(last or date.min).toordinal(), -passes, task)
    if rank_by == "span":
        return (-(span_days if span_days is not None else -1), -passes, task)
    return (-passes, task)


def _min_passes_depth(*, comparable_passes: int, passes: int, rank_by: str) -> int:
    """The depth ``min_passes`` gates a site on under ``rank_by``.

    The qualification twin of :func:`_rank_sort_key`: the floor is measured on the
    same quantity the ranking orders by, so ``min_passes`` and ``rank_by`` agree
    about what "depth" means. Under ``"comparable"`` a site qualifies on its
    *analysable* depth (``comparable_passes``, the largest single-polarization dated
    subset an analysis verb can actually difference), so ``--rank-by comparable
    --min-passes N`` means "sites whose differenceable series is at least ``N``
    passes deep" -- it can no longer admit a site whose raw count clears the floor
    but whose usable series falls short of it, the same "raw count overstates what is
    analysable" correction the comparable ranking makes, applied to the floor.
    Because ``comparable_passes <= passes`` this only ever *narrows* a comparable
    ranking (it never admits a site the raw floor rejected). Under ``"passes"`` it is
    the raw pass count, unchanged -- so the default discovery answer and the featured
    gallery are untouched. Single-sourced so :func:`select_featured_sites` (filtering
    a candidate pool) and :meth:`umbra_py.index.CatalogIndex.rank_sites` (filtering
    summarised :class:`SiteCoverage` records) cannot disagree about who qualifies.
    """
    return comparable_passes if rank_by == "comparable" else passes


def _check_max_revisit(max_revisit_days: float | None) -> None:
    """Reject a non-positive ``max_revisit`` cadence bound.

    A revisit gap between two distinct passes is always positive, so a
    non-positive bound could only ever return nothing -- a silent empty result of
    exactly the kind the rest of this surface refuses. ``None`` disables the
    filter (the default). Shared by every discovery surface so they cannot
    disagree about what a cadence bound is, exactly as :func:`_check_ranking`
    is for ``rank_by``.
    """
    if max_revisit_days is not None and max_revisit_days <= 0:
        raise ValueError(f"max_revisit_days must be positive (days), got {max_revisit_days!r}")


def _check_median_revisit(median_revisit_days: float | None) -> None:
    """Reject a non-positive ``median_revisit`` cadence bound.

    The *typical*-cadence twin of :func:`_check_max_revisit`: a revisit gap between
    two distinct passes is always positive, so a median gap is too, and a
    non-positive bound could only ever return nothing -- the silent empty result
    the rest of this surface refuses. ``None`` disables the filter (the default).
    Shared by every discovery surface so they cannot disagree about what a typical
    cadence bound is, exactly as :func:`_check_max_revisit` is for the worst-case one.
    """
    if median_revisit_days is not None and median_revisit_days <= 0:
        raise ValueError(
            f"median_revisit_days must be positive (days), got {median_revisit_days!r}"
        )


def _check_min_span(min_span_days: float | None) -> None:
    """Reject a non-positive ``min_span`` baseline bound.

    A site's observation span between two distinct passes is always positive, so a
    non-positive bound could only ever mean "keep everything" -- a silent no-op of
    exactly the kind the rest of this surface refuses. ``None`` disables the filter
    (the default). Shared by every discovery surface so they cannot disagree about
    what a span bound is, exactly as :func:`_check_max_revisit` is for
    ``max_revisit`` and :func:`_check_ranking` is for ``rank_by``.
    """
    if min_span_days is not None and min_span_days <= 0:
        raise ValueError(f"min_span_days must be positive (days), got {min_span_days!r}")


def _check_max_span(max_span_days: float | None) -> None:
    """Reject a non-positive ``max_span`` baseline bound.

    The upper twin of :func:`_check_min_span`: a site's observation span between two
    distinct passes is always positive, so a non-positive ceiling could only ever
    keep the degenerate same-day-only case (or nothing at all) -- not the honest
    "short-lived series" a ceiling is for -- which is the silent-no-op-adjacent
    surprise the rest of this surface refuses. ``None`` disables the filter (the
    default). Shared by every discovery surface so they cannot disagree about what a
    span bound is, exactly as :func:`_check_min_span` is for ``min_span``.
    """
    if max_span_days is not None and max_span_days <= 0:
        raise ValueError(f"max_span_days must be positive (days), got {max_span_days!r}")


def _passes_cadence(items: Iterable[UmbraItem], *, rank_by: str, max_revisit_days: float) -> bool:
    """Whether the series ``rank_by`` measures is revisited at least this often.

    The cadence twin of :func:`_min_passes_depth`: the gate measures the *same*
    series the ranking orders and qualifies by, so ``max_revisit`` and ``rank_by``
    agree about which cadence "worst-case" means. Under ``"comparable"`` it gates
    the largest single-polarization dated subset (the differenceable series the
    analysis verbs consume -- :func:`_largest_comparable_group`); under
    ``"passes"`` the whole dated series. Returns ``True`` iff that series'
    *worst-case* revisit gap -- its ``max_revisit_days`` /
    ``comparable_max_revisit_days`` figure, the widest stretch a change could have
    gone unseen -- is at most ``max_revisit_days`` days.

    A series with fewer than two passes has no measurable cadence, so it *fails*:
    a site whose revisit cannot be confirmed is not one a cadence filter should
    return, the same way ``active_since`` drops a site with no datable pass. The
    worst gap is computed from the same items by the same :func:`_revisit_days`
    that :func:`site_coverage` reduces, so filtering here can never disagree with
    the :attr:`SiteCoverage.max_revisit_days` a summary reports -- which is what
    lets the pool path (gating items in :func:`umbra_py.showcase.select_featured_sites`)
    and the index path (gating the same items in
    :meth:`umbra_py.index.CatalogIndex.rank_sites`) stay byte-identical.
    """
    series = _largest_comparable_group(items) if rank_by == "comparable" else items
    dates = sorted(i.datetime for i in series if i.datetime is not None)
    gaps = _revisit_days(dates)
    return bool(gaps) and max(gaps) <= max_revisit_days


def _passes_median_revisit(
    items: Iterable[UmbraItem], *, rank_by: str, median_revisit_days: float
) -> bool:
    """Whether the series ``rank_by`` measures is *typically* revisited this often.

    The typical-cadence twin of :func:`_passes_cadence`: same series, same
    :func:`_revisit_days` gaps, but it gates the *median* gap rather than the worst
    one. Under ``"comparable"`` it gates the largest single-polarization dated subset
    (:func:`_largest_comparable_group`, the differenceable series the analysis verbs
    consume); under ``"passes"`` the whole dated series. Returns ``True`` iff that
    series' *typical* revisit gap -- its ``median_revisit_days`` /
    ``comparable_median_revisit_days`` figure, the gap a site is *usually* imaged at
    -- is at most ``median_revisit_days`` days.

    It selects a different question from :func:`_passes_cadence`: where the worst-gap
    bound (``max_revisit``) drops a site the moment *any* stretch exceeds the bound
    (never blind for longer than ``N`` days), the median bound tolerates the odd long
    gap and keeps a site *usually* imaged often -- so a mostly-tight series with a
    single outage passes the median filter but fails the worst-case one. The report
    carries both figures, so the choice is only about which one the *filter* reads.

    A series with fewer than two passes has no measurable cadence, so it *fails*: a
    site whose revisit cannot be confirmed is not one a cadence filter should return,
    exactly as :func:`_passes_cadence` drops it. The median is computed from the same
    items by the same :func:`_revisit_days` and :func:`statistics.median` that
    :func:`site_coverage` reduces, so filtering here can never disagree with the
    :attr:`SiteCoverage.median_revisit_days` a summary reports -- which is what lets
    the pool path (gating items in :func:`umbra_py.showcase.select_featured_sites`)
    and the index path (gating the same items in
    :meth:`umbra_py.index.CatalogIndex.rank_sites`) stay byte-identical.
    """
    series = _largest_comparable_group(items) if rank_by == "comparable" else items
    dates = sorted(i.datetime for i in series if i.datetime is not None)
    gaps = _revisit_days(dates)
    return bool(gaps) and median(gaps) <= median_revisit_days


def _passes_span(items: Iterable[UmbraItem], *, rank_by: str, min_span_days: float) -> bool:
    """Whether the series ``rank_by`` measures spans at least this long.

    The baseline twin of :func:`_passes_cadence`: the gate measures the *same*
    series the ranking orders and qualifies by, so ``min_span`` and ``rank_by``
    agree about which observation window "span" means. Under ``"comparable"`` it
    gates the largest single-polarization dated subset (the differenceable series
    the analysis verbs consume -- :func:`_largest_comparable_group`); under
    ``"passes"`` the whole dated series. Returns ``True`` iff that series' baseline
    -- its ``span_days`` / ``comparable_span_days`` figure, whole days from its
    first dated pass to its last -- is at least ``min_span_days`` days.

    Span is a different axis from cadence: ``max_revisit`` bounds the *worst gap*
    between consecutive passes (how reliably a site is watched), whereas this bounds
    the *total baseline* (how long it has been watched at all). A site imaged ten
    times in one week has a tight cadence but a short span; one imaged once a year
    for five years has a loose cadence but a long span -- the observation window a
    slow change (subsidence, construction, deforestation) needs to be visible in.

    A series with fewer than two dated passes has no measurable span, so it *fails*:
    a site whose baseline cannot be confirmed is not one a span filter should
    return, the same way :func:`_passes_cadence` drops a site with no measurable
    cadence and ``active_since`` drops one with no datable pass. The span is
    computed exactly as :func:`site_coverage` reduces ``span_days`` (whole days
    between the first and last dated pass), so filtering here can never disagree
    with the :attr:`SiteCoverage.span_days` a summary reports -- which is what lets
    the pool path (gating items in :func:`umbra_py.showcase.select_featured_sites`)
    and the index path (gating the same items in
    :meth:`umbra_py.index.CatalogIndex.rank_sites`) stay byte-identical.
    """
    series = _largest_comparable_group(items) if rank_by == "comparable" else items
    dates = sorted(i.datetime for i in series if i.datetime is not None)
    if len(dates) < 2:
        return False
    return (dates[-1] - dates[0]).days >= min_span_days


def _passes_max_span(items: Iterable[UmbraItem], *, rank_by: str, max_span_days: float) -> bool:
    """Whether the series ``rank_by`` measures spans at most this long.

    The upper twin of :func:`_passes_span`: same series, same figure, but a *ceiling*
    on the baseline rather than a floor. Under ``"comparable"`` it gates the largest
    single-polarization dated subset (:func:`_largest_comparable_group`); under
    ``"passes"`` the whole dated series. Returns ``True`` iff that series' baseline --
    its ``span_days`` / ``comparable_span_days`` figure, whole days from its first
    dated pass to its last -- is at most ``max_span_days`` days.

    It selects the complement of ``min_span``: where a floor keeps the long-baseline
    series a *slow* change needs, a ceiling keeps the *short-lived* one -- a burst of
    imaging over a narrow window, now over. Set with ``min_span`` the two bound the
    baseline to a window (``min_span_days <= span <= max_span_days``), exactly as
    ``active_since`` / ``active_before`` bound the newest pass to one.

    A series with fewer than two dated passes has no measurable span, so it *fails* --
    the same rule :func:`_passes_span` applies, which is what lets ``min_span`` and
    ``max_span`` compose as a clean window (both admit only a *confirmed* span) rather
    than one dropping the unmeasurable and the other keeping it. The span is computed
    exactly as :func:`site_coverage` reduces ``span_days``, so filtering here can never
    disagree with the :attr:`SiteCoverage.span_days` a summary reports -- which is what
    lets the pool path (gating items in :func:`umbra_py.showcase.select_featured_sites`)
    and the index path (gating the same items in
    :meth:`umbra_py.index.CatalogIndex.rank_sites`) stay byte-identical.
    """
    series = _largest_comparable_group(items) if rank_by == "comparable" else items
    dates = sorted(i.datetime for i in series if i.datetime is not None)
    if len(dates) < 2:
        return False
    return (dates[-1] - dates[0]).days <= max_span_days


@dataclass(frozen=True)
class SiteCoverage:
    """A repeat-imaged site's coverage, reduced to the facts that decide whether
    it is worth analysing.

    The revisit figures are the gaps between consecutive dated passes, in days;
    they are ``None`` for a site with fewer than two dated passes (no gap to
    measure). ``max_revisit_days`` is the *longest* such gap -- the site's
    worst-case temporal resolution, the widest stretch a change could have
    happened in unseen; read together with ``median_revisit_days`` it separates a
    site imaged on a steady cadence (``max`` near the median) from a bursty or
    thinning one (``max`` far above it). ``comparable_passes`` is how many of the
    passes can actually be *differenced* together: ``passes`` counts every
    acquisition, but every analysis verb (``change`` / ``timescan`` / ``stack``,
    ``stack_stats``, ``change --narrate``) refuses a mixed-polarization selection,
    and an undated pass cannot be ordered onto a time axis at all, so the honest
    depth of a change series here is the largest set of *dated* passes sharing one
    polarization -- the pool
    :func:`umbra_py.viz.composites.select_change_frames` draws from. When it is
    below ``passes`` the raw count overstates what is analysable (a mix of
    polarizations, or undated passes riding along); ``polarizations`` names which
    polarizations are present, this says how deep the biggest comparable subset
    is. ``comparable_span_days`` is that subset's *temporal* reach -- whole days
    from the comparable group's first pass to its last -- where ``span_days`` (and
    the revisit figures) measure the whole dated range, including off-polarization
    or undated passes no analysis verb can difference against the rest. Below
    ``span_days`` it means passes outside the comparable group stretch the full
    range past the window the analysable series actually covers, the temporal
    twin of ``comparable_passes`` undercutting ``passes``; ``None`` with fewer
    than two comparable passes, exactly as ``span_days`` is with fewer than two
    dated ones. ``comparable_max_revisit_days`` is that same subset's *cadence*:
    the longest gap between two consecutive passes of the comparable group -- the
    widest stretch a change could have gone unseen *in the series that can
    actually be differenced*, where ``max_revisit_days`` measures the gaps of the
    whole dated range. The two can disagree either way, and both directions are a
    correction: a cross-polarization pass landing inside a gap makes the raw
    cadence look *tighter* than the analysable series is (raw below comparable),
    while a wide gap between off-polarization passes -- irrelevant to a
    single-polarization change run -- can inflate the raw figure past anything in
    the comparable series (raw above comparable). So it is the worst-case revisit
    of the passes ``comparable_hrefs`` hands onward, the cadence counterpart of
    ``comparable_span_days``; ``None`` with fewer than two comparable passes, as
    ``max_revisit_days`` is with fewer than two dated ones.
    ``comparable_min_revisit_days`` and ``comparable_median_revisit_days`` are the
    tightest and typical gaps of that same comparable group -- the twins of
    ``min_revisit_days`` / ``median_revisit_days`` -- so the whole revisit cadence
    (shortest, typical, worst) can be read over the series a change run can
    actually difference rather than over the raw dated range an off-polarization
    pass distorts either way. Each equals its raw counterpart when every dated pass
    shares one polarization, and each is ``None`` with fewer than two comparable
    passes, exactly as its raw counterpart is with fewer than two dated ones. With
    the three the comparable cadence is complete: every raw coverage figure now has
    an analysable-series twin. ``comparable_polarizations`` names *which*
    polarization that analysable series is: the shared signature every pass of the
    comparable group carries -- the group key :func:`_largest_comparable_group`
    selects on -- where ``polarizations`` lists every polarization present across
    the *whole* site. A strict subset of ``polarizations`` says which single
    signature the ``comparable_passes`` depth, ``comparable_span_days`` reach and
    comparable cadence are all measured over, and which one a ``--pol``-style
    filter would keep to reproduce ``comparable_hrefs``; equal to ``polarizations``
    when every dated pass already shares one signature. It is an empty tuple when
    the comparable group's passes carry no polarization metadata (the
    empty-signature group, exactly as ``polarizations`` is empty then) and when no
    pass is dated at all (``comparable_passes`` is ``0``, so there is no group to
    name) -- the two told apart by ``comparable_passes``, never ``None`` (a
    signature is a set that can be empty, like ``polarizations``, not a scalar that
    can be absent). ``bbox`` is the union
    footprint of every pass with one, so it is the
    rectangle a follow-up ``--bbox`` / ``--intersects`` would cover. ``hrefs`` is
    every pass oldest-first, the order ``umbra change`` / ``umbra stack`` want
    their passes in; ``comparable_hrefs`` is the subset of those URLs belonging to
    the ``comparable_passes`` group -- the exact passes that will *not* trip the
    mixed-polarization refusal, so it is the selection to hand an analysis verb
    straight through, where ``hrefs`` is the whole roster to choose from.
    ``len(comparable_hrefs)`` equals ``comparable_passes`` (barring a pass with no
    URL, which ``hrefs`` already drops); when the two href lists are equal every
    dated pass is already comparable.
    """

    task: str
    label: str
    passes: int
    comparable_passes: int
    first: str | None
    last: str | None
    span_days: int | None
    comparable_span_days: int | None
    min_revisit_days: float | None
    median_revisit_days: float | None
    max_revisit_days: float | None
    comparable_min_revisit_days: float | None
    comparable_median_revisit_days: float | None
    comparable_max_revisit_days: float | None
    bbox: BBox | None
    products: tuple[str, ...]
    polarizations: tuple[str, ...]
    comparable_polarizations: tuple[str, ...]
    hrefs: tuple[str, ...]
    comparable_hrefs: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """A JSON-ready mapping (``umbra sites --json``), field order preserved."""
        return {
            "task": self.task,
            "label": self.label,
            "passes": self.passes,
            "comparable_passes": self.comparable_passes,
            "first": self.first,
            "last": self.last,
            "span_days": self.span_days,
            "comparable_span_days": self.comparable_span_days,
            "min_revisit_days": self.min_revisit_days,
            "median_revisit_days": self.median_revisit_days,
            "max_revisit_days": self.max_revisit_days,
            "comparable_min_revisit_days": self.comparable_min_revisit_days,
            "comparable_median_revisit_days": self.comparable_median_revisit_days,
            "comparable_max_revisit_days": self.comparable_max_revisit_days,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "products": list(self.products),
            "polarizations": list(self.polarizations),
            "comparable_polarizations": list(self.comparable_polarizations),
            "hrefs": list(self.hrefs),
            "comparable_hrefs": list(self.comparable_hrefs),
        }


def _union_bbox(items: Iterable[UmbraItem]) -> BBox | None:
    """The bounding rectangle covering every pass that has a footprint."""
    boxes = [i.bbox for i in items if i.bbox is not None]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _largest_comparable_group(items: Iterable[UmbraItem]) -> list[UmbraItem]:
    """The dated passes of the largest single-polarization signature, oldest-first.

    This is the pool :func:`umbra_py.viz.composites.select_change_frames` selects
    for a composite -- dated passes grouped by their polarization tuple, largest
    group wins, ties broken by the polarization tuple (the same order the frame
    selector uses, so the two cannot pick different passes) -- so it is the deepest
    change series the site supports before the mixed-polarization refusal the
    analysis verbs enforce. Undated passes are excluded (they cannot be ordered
    onto a time axis); a pass carrying no polarization metadata groups with other
    such passes (the empty tuple), which is exactly how the frame selector treats
    them. Empty when nothing is dated.

    Its length is ``comparable_passes`` and its passes' URLs are
    ``comparable_hrefs``, both derived from this one group so the count and the
    selection cannot disagree.
    """
    groups: dict[tuple[str, ...], list[UmbraItem]] = {}
    for item in items:
        if item.datetime is None:
            continue
        groups.setdefault(tuple(item.polarizations), []).append(item)
    if not groups:
        return []
    # Largest group, ties broken by the polarization tuple -- select_change_frames'
    # own deterministic order -- then sorted oldest-first (all members are dated).
    winner = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][1]
    return sorted(winner, key=lambda i: i.datetime or datetime.min)


def _revisit_days(dates: Sequence[datetime]) -> list[float]:
    """Gaps in days between consecutive (already sorted) acquisition times."""
    return [
        (later - earlier).total_seconds() / 86400.0
        for earlier, later in zip(dates, dates[1:], strict=False)
    ]


def _temporal_rank_figures(items: Iterable[UmbraItem]) -> tuple[date | None, int | None]:
    """The ``(newest-pass date, span in days)`` a ``"recency"`` / ``"span"`` ranking
    sorts on, computed exactly as :func:`site_coverage` reduces ``last`` / ``span_days``.

    Reducing these from the items in one shared place is what lets the pool path
    (:func:`umbra_py.showcase.select_featured_sites`, which ranks
    :class:`umbra_py.showcase.FeaturedSite` before summarising) feed
    :func:`_rank_sort_key` the same figures the index path reads back off a
    :class:`SiteCoverage` -- so the two rankings cannot disagree. Returns
    ``(None, None)`` for a site with no dated pass, and a ``None`` span for one with a
    single dated pass (no baseline to measure), matching the summary fields.
    """
    dated = sorted(i.datetime for i in items if i.datetime is not None)
    if not dated:
        return None, None
    span = (dated[-1] - dated[0]).days if len(dated) >= 2 else None
    return dated[-1].date(), span


def site_coverage(
    task: str, items: Sequence[UmbraItem], *, label: str | None = None
) -> SiteCoverage:
    """Summarise one site's passes into a :class:`SiteCoverage`.

    ``items`` need not be sorted; the summary orders them oldest-first (undated
    passes last, so their URLs still ride along). ``label`` defaults to the first
    pass that carries a baked place name (``umbra index bake``), else the task
    codename -- the same rule :attr:`umbra_py.showcase.FeaturedSite.label` uses,
    passed in by :func:`rank_site_coverage` so the two cannot drift.
    """
    ordered = sorted(items, key=lambda i: (i.datetime is None, i.datetime or datetime.min))
    dated = [i.datetime for i in ordered if i.datetime is not None]
    if label is None:
        label = next((i.place for i in ordered if i.place), task)

    first = dated[0].date().isoformat() if dated else None
    last = dated[-1].date().isoformat() if dated else None
    span_days = (dated[-1] - dated[0]).days if len(dated) >= 2 else None
    gaps = _revisit_days(dated)
    products = sorted({a for i in ordered for a in i.available_assets})
    pols = sorted({p for i in ordered for p in i.polarizations})
    hrefs = tuple(i.href for i in ordered if i.href)
    comparable = _largest_comparable_group(ordered)
    # The signature that *defines* that group: every member shares one polarization
    # tuple (it is the key `_largest_comparable_group` grouped on), so the first
    # member's is the group's -- taken from the group itself rather than recomputed,
    # so the name and the passes it names cannot disagree. Empty tuple when the
    # group has no polarization metadata (the empty-signature group) or no group
    # exists (nothing dated), the two told apart by `comparable_passes`.
    comparable_polarizations = tuple(comparable[0].polarizations) if comparable else ()
    # The comparable group is dated and oldest-first by construction, so its span
    # is the analysable series' temporal reach -- measured over that subset, not
    # the whole dated range, so bracketing off-polarization passes cannot inflate
    # it (the temporal twin of comparable_passes undercutting passes).
    comparable_dates = [i.datetime for i in comparable if i.datetime is not None]
    comparable_span_days = (
        (comparable_dates[-1] - comparable_dates[0]).days if len(comparable_dates) >= 2 else None
    )
    # The cadence of that same subset: gaps between consecutive comparable passes,
    # so a cross-polarization pass filling a gap in the raw dated series cannot
    # distort the analysable series' revisit figures. Reduced to the same
    # shortest / typical / worst triple as the raw gaps, so the whole cadence can
    # be read over the differenceable series and not only the raw dated range.
    comparable_gaps = _revisit_days(comparable_dates)

    return SiteCoverage(
        task=task,
        label=label,
        passes=len(ordered),
        comparable_passes=len(comparable),
        first=first,
        last=last,
        span_days=span_days,
        comparable_span_days=comparable_span_days,
        min_revisit_days=min(gaps) if gaps else None,
        median_revisit_days=median(gaps) if gaps else None,
        max_revisit_days=max(gaps) if gaps else None,
        comparable_min_revisit_days=min(comparable_gaps) if comparable_gaps else None,
        comparable_median_revisit_days=median(comparable_gaps) if comparable_gaps else None,
        comparable_max_revisit_days=max(comparable_gaps) if comparable_gaps else None,
        bbox=_union_bbox(ordered),
        products=tuple(products),
        polarizations=tuple(pols),
        comparable_polarizations=comparable_polarizations,
        hrefs=hrefs,
        comparable_hrefs=tuple(i.href for i in comparable if i.href),
    )


def rank_site_coverage(
    items: Iterable[UmbraItem],
    *,
    top: int = 20,
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
) -> list[SiteCoverage]:
    """The most repeat-imaged sites in ``items``, best-first, each summarised.

    The ranking is :func:`umbra_py.showcase.select_featured_sites` exactly -- sites
    ordered by ``rank_by`` (descending) then task name, keeping those with at least
    ``min_passes`` dated passes -- so ``umbra sites`` and the showcase's featured
    gallery agree on what "most repeat-imaged" means. Only the summarisation is
    new here. Deterministic and offline: it calls no renderer, no model and no
    network.

    ``rank_by`` is one of :data:`SITE_RANKINGS`. The two *depth* rankings are
    ``"passes"`` (the default -- raw pass count) and ``"comparable"`` (the site's
    *analysable* depth, i.e. the ``comparable_passes`` largest single-polarization
    dated subset a change verb can actually difference); the two coincide when every
    dated pass of every site shares one polarization and diverge when a raw count
    overstates what is analysable, which is exactly when the discovery answer should
    prefer the deeper differenceable series. The two *temporal* rankings are
    ``"recency"`` (each site's **newest** dated pass first -- the still-active site a
    monitoring or tasking user wants, which a depth ranking buries under a deeper
    series that has gone dormant) and ``"span"`` (each site's observation
    **baseline** first -- the long-watched site a *slow* change needs), ordering by
    the whole-site ``last`` / ``span_days`` a summary reports, ties broken by raw
    depth then task. So the moat now ranks on every axis it already filters on
    (recency via ``active_*``, baseline via ``min_span`` / ``max_span``), not only on
    depth. ``select_featured_sites`` applies the chosen key *before* truncating to
    ``top``, so a site the ranking would promote is not dropped by a raw-count prefix
    first (a recently-active or long-baseline site outside the raw top-``top`` still
    surfaces). ``min_passes`` qualifies a site on the depth ``rank_by`` measures --
    raw pass count under ``"passes"`` / ``"recency"`` / ``"span"``, analysable depth
    under ``"comparable"`` -- so a temporal ranking still keeps only sites deep enough
    to have a series worth ordering.

    ``min_passes`` measures the same depth ``rank_by`` does (:func:`_min_passes_depth`):
    under ``"comparable"`` a site must have at least ``min_passes`` *comparable*
    passes to qualify, so ``rank_by="comparable", min_passes=N`` means "sites whose
    differenceable series is at least ``N`` passes deep" rather than "sites with
    ``N`` raw passes, ranked by their usable depth". Under ``"passes"`` it is the raw
    dated pass count, unchanged.

    ``active_since`` keeps only sites still being imaged *on or after* that date --
    a recency filter on each site's **newest** dated pass, so a deep series that
    stopped long ago is dropped while an actively-revisited one survives (an ISO
    date, a bare year/month, or a relative expression like ``"6 months ago"``; see
    :func:`umbra_py.dates.parse_date_bound`). It is orthogonal to ``rank_by`` /
    ``min_passes`` (it gates on the site's latest pass, whatever depth the ranking
    measures) and distinct from ``start`` / ``end`` (those bound which *passes*
    enter the pool; this selects whole sites by recency and keeps each survivor's
    full history). ``None`` (the default) applies no recency filter.

    ``active_before`` is the complement -- keep only sites whose newest dated pass
    is *on or before* that date (a dormant series that stopped imaging), so with
    ``active_since`` it selects sites whose latest pass falls within a window. A span
    expression snaps to its last day (``active_before="2024"`` is "last imaged on or
    before 2024-12-31"), symmetric with ``end``. ``None`` applies no upper bound.

    ``first_since`` / ``first_before`` are the onset (first-seen) twins of the
    ``active_*`` pair: where ``active_since`` / ``active_before`` gate a site's
    **newest** dated pass (is it still, or no longer, being imaged), these gate its
    **earliest** one (when did it *start*). ``first_since`` keeps only sites whose
    first dated pass is *on or after* that date -- a **newly-appeared** series, one
    that entered the archive recently -- and ``first_before`` only those whose first
    pass is *on or before* it -- a **long-established** series watched since before
    then; set together they bound the onset to a window
    (``first_since <= first <= first_before``), exactly as the ``active_*`` pair
    bounds the newest pass. They accept the same grammar the recency bounds do, and
    ``first_before`` snaps a span expression to its last day (``first_before="2024"``
    is "first imaged on or before 2024-12-31"), symmetric with ``active_before`` /
    ``end``. Both gate on the whole site's earliest pass (the ``first`` a summary
    reports), independent of ``rank_by`` / ``min_passes`` and distinct from ``start``
    / ``end`` (which bound which *passes* enter the pool, whereas these select whole
    sites by onset and keep each survivor's full history). ``None`` (the default)
    applies no onset filter.

    ``max_revisit_days`` keeps only sites revisited *at least this often* -- a
    cadence filter on each site's **worst-case** revisit gap, so a site with any
    stretch longer than ``max_revisit_days`` days between consecutive passes is
    dropped and one imaged reliably is kept. It is the selection twin of the
    ``max_revisit_days`` figure the summary already reports, and it measures the
    same depth ``rank_by`` does (:func:`_passes_cadence`): under ``"comparable"``
    it gates the *analysable* series' worst gap (``comparable_max_revisit_days``),
    so a site whose raw cadence looks tight only because an off-polarization pass
    fills a gap no change verb can use is not admitted, the cadence counterpart of
    ``min_passes`` gating comparable depth. It is orthogonal to ``active_since`` /
    ``active_before`` (recency of the newest pass) and distinct from ``start`` /
    ``end`` (which bound which passes enter the pool). A site with fewer than two
    passes in the gated series has no measurable cadence and is dropped. ``None``
    (the default) applies no cadence filter; a non-positive value is a
    ``ValueError``.

    ``median_revisit_days`` is the *typical*-cadence twin of ``max_revisit_days`` --
    keep only sites whose **median** revisit gap is at most that many days, so a
    site *usually* imaged often is kept even if a single stretch runs long, where
    ``max_revisit_days`` drops it the moment *any* gap exceeds the bound. It is the
    selection twin of the ``median_revisit_days`` figure the summary already reports,
    and measures the same series ``rank_by`` does (:func:`_passes_median_revisit`):
    under ``"comparable"`` the *analysable* series' typical gap
    (``comparable_median_revisit_days``), so an off-polarization pass filling a gap
    no change verb can use cannot make a site read as more regularly imaged than it
    is. It is orthogonal to ``max_revisit_days`` (worst gap vs typical gap -- set both
    for "usually imaged every A days and never blind longer than B"), to the recency
    bounds and to ``start`` / ``end``. A site with fewer than two passes in the gated
    series has no measurable cadence and is dropped. ``None`` (the default) applies no
    typical-cadence filter; a non-positive value is a ``ValueError``.

    ``min_span_days`` keeps only sites imaged over *at least this long* -- a
    baseline filter on each site's observation **span** (whole days from its first
    dated pass to its last), so a series confined to a short window is dropped and a
    long-baseline one kept. It is the selection twin of the ``span_days`` figure the
    summary already reports, and a different axis from ``max_revisit_days``: cadence
    is the worst *gap* (how reliably a site is watched), span is the total *baseline*
    (how long it has been watched at all), so it is the discovery answer for slow
    change that needs a long window to show -- subsidence, construction,
    deforestation. It measures the same series ``rank_by`` does
    (:func:`_passes_span`): under ``"comparable"`` the *analysable* series' span
    (``comparable_span_days``), so off-polarization passes bracketing the range
    cannot make a site's baseline look longer than the series a change verb can
    difference. It is orthogonal to ``active_since`` / ``active_before`` (recency of
    the newest pass) and to ``max_revisit_days`` (cadence), and distinct from
    ``start`` / ``end`` (which bound which passes enter the pool). A site with fewer
    than two passes in the gated series has no measurable span and is dropped.
    ``None`` (the default) applies no span filter; a non-positive value is a
    ``ValueError``.

    ``max_span_days`` is the upper twin of ``min_span_days`` -- keep only sites imaged
    over *at most this long*, so a short-window series is kept and a long-baseline one
    dropped. It selects the complement of ``min_span_days`` (the *short-lived* series,
    a burst of imaging now over, rather than the long-baseline one a slow change
    needs), and set with ``min_span_days`` the two bound each site's baseline to a
    window (``min_span_days <= span <= max_span_days``), exactly as ``active_since`` /
    ``active_before`` bound the newest pass to one. It measures the same series
    ``rank_by`` does (:func:`_passes_max_span`): under ``"comparable"`` the *analysable*
    series' span, so off-polarization passes bracketing the range cannot make a site's
    baseline read longer -- and so escape a ceiling -- than the series a change verb
    can difference. Like ``min_span_days`` it drops a site with fewer than two passes
    in the gated series (no confirmed baseline to admit), which is what lets the two
    compose as a clean window. ``None`` (the default) applies no span ceiling; a
    non-positive value is a ``ValueError``.
    """
    from .showcase import select_featured_sites  # noqa: PLC0415

    _check_ranking(rank_by)
    _check_max_revisit(max_revisit_days)
    _check_median_revisit(median_revisit_days)
    _check_min_span(min_span_days)
    _check_max_span(max_span_days)
    sites = select_featured_sites(
        items,
        count=top,
        min_passes=min_passes,
        rank_by=rank_by,
        active_since=active_since,
        active_before=active_before,
        first_since=first_since,
        first_before=first_before,
        max_revisit_days=max_revisit_days,
        median_revisit_days=median_revisit_days,
        min_span_days=min_span_days,
        max_span_days=max_span_days,
    )
    return [site_coverage(s.task, s.items, label=s.label) for s in sites]
