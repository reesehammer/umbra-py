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

from .models import BBox, UmbraItem


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
    is. ``bbox`` is the union footprint of every pass with one, so it is the
    rectangle a follow-up ``--bbox`` / ``--intersects`` would cover. ``hrefs`` is
    oldest-first, the order ``umbra change`` / ``umbra stack`` want their passes
    in.
    """

    task: str
    label: str
    passes: int
    comparable_passes: int
    first: str | None
    last: str | None
    span_days: int | None
    min_revisit_days: float | None
    median_revisit_days: float | None
    max_revisit_days: float | None
    bbox: BBox | None
    products: tuple[str, ...]
    polarizations: tuple[str, ...]
    hrefs: tuple[str, ...]

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
            "min_revisit_days": self.min_revisit_days,
            "median_revisit_days": self.median_revisit_days,
            "max_revisit_days": self.max_revisit_days,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "products": list(self.products),
            "polarizations": list(self.polarizations),
            "hrefs": list(self.hrefs),
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


def _largest_comparable_group(items: Iterable[UmbraItem]) -> int:
    """How many dated passes share the largest single-polarization signature.

    This is the pool :func:`umbra_py.viz.composites.select_change_frames` selects
    for a composite -- dated passes grouped by their polarization tuple, largest
    group wins -- so it is the deepest change series the site supports before the
    mixed-polarization refusal the analysis verbs enforce. Undated passes are
    excluded (they cannot be ordered onto a time axis); a pass carrying no
    polarization metadata groups with other such passes (the empty tuple), which
    is exactly how the frame selector treats them. Zero when nothing is dated.
    """
    groups: dict[tuple[str, ...], int] = {}
    for item in items:
        if item.datetime is None:
            continue
        groups[tuple(item.polarizations)] = groups.get(tuple(item.polarizations), 0) + 1
    return max(groups.values(), default=0)


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

    return SiteCoverage(
        task=task,
        label=label,
        passes=len(ordered),
        comparable_passes=_largest_comparable_group(ordered),
        first=first,
        last=last,
        span_days=span_days,
        min_revisit_days=min(gaps) if gaps else None,
        median_revisit_days=median(gaps) if gaps else None,
        max_revisit_days=max(gaps) if gaps else None,
        bbox=_union_bbox(ordered),
        products=tuple(products),
        polarizations=tuple(pols),
        hrefs=hrefs,
    )


def rank_site_coverage(
    items: Iterable[UmbraItem], *, top: int = 20, min_passes: int = 2
) -> list[SiteCoverage]:
    """The most repeat-imaged sites in ``items``, best-first, each summarised.

    The ranking is :func:`umbra_py.showcase.select_featured_sites` exactly -- sites
    ordered by pass count (descending) then task name, keeping those with at least
    ``min_passes`` dated passes -- so ``umbra sites`` and the showcase's featured
    gallery agree on what "most repeat-imaged" means. Only the summarisation is
    new here. Deterministic and offline: it calls no renderer, no model and no
    network.
    """
    from .showcase import select_featured_sites  # noqa: PLC0415

    sites = select_featured_sites(items, count=top, min_passes=min_passes)
    return [site_coverage(s.task, s.items, label=s.label) for s in sites]
