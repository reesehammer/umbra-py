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
from datetime import datetime
from statistics import median
from typing import TYPE_CHECKING

from .models import BBox, UmbraItem

if TYPE_CHECKING:
    from .catalog import DateLike

#: The orderings :func:`rank_site_coverage` (and every discovery surface that
#: forwards to it) can rank sites by. ``"passes"`` is the historical default --
#: raw pass count, most-imaged first -- which is what the static showcase's
#: featured gallery wants (more acquisitions to precompute, whatever their
#: polarization mix). ``"comparable"`` ranks by *analysable* depth instead: the
#: ``comparable_passes`` figure, i.e. the largest single-polarization dated subset
#: an analysis verb (``change`` / ``timescan`` / ``stack``, ``stack_stats``,
#: ``change --narrate``) can actually difference. The two disagree exactly when a
#: raw count overstates what is analysable -- a site whose passes span several
#: polarizations, or carry undated passes -- so a shallow-but-broad site can
#: outrank a deep single-polarization series under ``"passes"`` yet fall behind it
#: under ``"comparable"``. The report has distinguished the two figures since the
#: comparable-figure workstream; this lets the *ranking* distinguish them too.
SITE_RANKINGS: tuple[str, ...] = ("passes", "comparable")


def _check_ranking(rank_by: str) -> None:
    """Reject an unknown ``rank_by``, naming the accepted set (shared by every
    surface so they cannot disagree about what a ranking is)."""
    if rank_by not in SITE_RANKINGS:
        raise ValueError(f"rank_by must be one of {SITE_RANKINGS}, got {rank_by!r}")


def _rank_sort_key(
    *, comparable_passes: int, passes: int, task: str, rank_by: str
) -> tuple[object, ...]:
    """The deterministic sort key for one site under ``rank_by``.

    ``"passes"`` orders by raw pass count then task name (the historical key
    :func:`umbra_py.showcase.select_featured_sites` has always used). ``"comparable"``
    orders by analysable depth first, breaking ties by raw pass count (so among
    equally-analysable sites the one with more total context ranks higher) and then
    by task name for full determinism. Both are ascending over negated counts, so a
    plain ``sort`` puts the best-covered site first. Single-sourced so
    :func:`select_featured_sites` (ranking ``FeaturedSite`` before summarising) and
    :meth:`umbra_py.index.CatalogIndex.rank_sites` (ranking summarised
    :class:`SiteCoverage` records) cannot pick a different order.
    """
    if rank_by == "comparable":
        return (-comparable_passes, -passes, task)
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
) -> list[SiteCoverage]:
    """The most repeat-imaged sites in ``items``, best-first, each summarised.

    The ranking is :func:`umbra_py.showcase.select_featured_sites` exactly -- sites
    ordered by ``rank_by`` (descending) then task name, keeping those with at least
    ``min_passes`` dated passes -- so ``umbra sites`` and the showcase's featured
    gallery agree on what "most repeat-imaged" means. Only the summarisation is
    new here. Deterministic and offline: it calls no renderer, no model and no
    network.

    ``rank_by`` is one of :data:`SITE_RANKINGS`: ``"passes"`` (the default -- raw
    pass count) or ``"comparable"`` (the site's *analysable* depth, i.e. the
    ``comparable_passes`` largest single-polarization dated subset a change verb can
    actually difference). The two coincide when every dated pass of every site
    shares one polarization; they diverge when a raw count overstates what is
    analysable, which is exactly when the discovery answer should prefer the deeper
    differenceable series. ``select_featured_sites`` applies the same key *before*
    truncating to ``top``, so a deeply-analysable site outside the raw top-``top`` is
    not dropped before the comparable ranking can promote it.

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
    """
    from .showcase import select_featured_sites  # noqa: PLC0415

    _check_ranking(rank_by)
    sites = select_featured_sites(
        items, count=top, min_passes=min_passes, rank_by=rank_by, active_since=active_since
    )
    return [site_coverage(s.task, s.items, label=s.label) for s in sites]
