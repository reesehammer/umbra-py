"""``umbra ask``: natural-language search that a model *plans* and the library
*executes* deterministically.

This is the capstone of the C1 natural-language-search direction (see
``docs/STRATEGY.md``). The two earlier steps stayed entirely
inside the library's determinism boundary -- relative dates
(:mod:`umbra_py.dates`) and fuzzy task matching (:mod:`umbra_py.fuzzy`) turn
natural language into a filter with *no model call*. ``umbra ask`` is the honest
way to add the model without giving up that boundary:

1. The user's sentence plus the :func:`umbra_py.llm_context` domain document go
   to a configured model (Anthropic or any OpenAI-compatible endpoint, with a
   user-supplied key). **The model only plans** -- it returns the search
   *parameters* it thinks the sentence maps to, as one JSON object.
2. Every field of that object is then re-validated by the deterministic layer
   (:func:`parse_plan`): dates go through :func:`umbra_py.parse_date_bound`,
   product types are checked against :data:`umbra_py.PRODUCT_ASSETS`, the bbox
   is range-checked, and the SAR acquisition-property filters (polarizations,
   incidence bounds, max resolution) are coerced and cross-checked. **Nothing the
   model says becomes a filter without passing through this validation**, so a
   hallucinated date, product type, or out-of-order incidence bound is an error,
   not a silent bad query.
3. The resolved, deterministic ``umbra search`` command is *shown* before it
   runs, so the user audits the plan. The LLM plans; the library executes; the
   user audits.

This module is the model boundary of the package. The deterministic pieces
(:func:`build_messages`, :func:`parse_plan`, :func:`plan_to_argv`,
:func:`plan_to_command`) are stdlib-only and fully offline-testable; the model
call is an injectable :data:`Planner` callable, so tests never touch the
network. The default planner is built from environment variables and uses only
:mod:`requests` (already a core dependency) -- no heavy SDK. The whole feature
lives behind the ``[ai]`` extra and never runs implicitly: only ``umbra ask``
reaches a model, and only when the user invokes it with a key configured.

Range keywords with hemisphere-dependent meaning (``"last winter"``) that the
deterministic :func:`umbra_py.parse_date_bound` intentionally rejects belong
here: the model resolves the season to concrete dates, which the deterministic
layer then validates like any other date.

**Areas of interest are chosen, never authored.** Every other search surface can
filter by a polygon (``search(intersects=…)``, ``umbra search --intersects``,
``POST /search``), but a plan had only ``bbox``/``place`` -- so "scenes over this
watershed" could only ever resolve to the rectangle around it. The gap was not an
oversight: a hallucinated date is caught by :func:`umbra_py.parse_date_bound`,
whereas a hallucinated ring is a *plausible* polygon over the wrong ground, and
nothing downstream can tell. So the model never emits coordinates. The caller
supplies the areas of interest it already has (``umbra ask --aoi coast.geojson``,
:class:`AreaOfInterest`), the prompt lists them **by name**, and the plan's
``aoi`` field is validated against that closed set -- an unknown name is an
:class:`AskError`, exactly like an unknown product type. The model picks which
shape the sentence meant; the shape itself is the user's file.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ._geometry import Geometry, geometry_bbox, to_geojson
from .constants import (
    OPENROUTER_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_HEADERS,
    PRODUCT_ASSETS,
)
from .context import llm_context
from .dates import parse_date_bound
from .exceptions import MissingDependencyError, UmbraError

__all__ = [
    "AreaOfInterest",
    "AskError",
    "SearchPlan",
    "Planner",
    "build_messages",
    "parse_plan",
    "plan_to_argv",
    "plan_to_command",
    "default_planner",
    "ask",
]

#: A planner turns the prompt (``{"system": str, "user": str}``) into the
#: model's raw text reply. Injectable so tests never call a model; the default
#: implementation is :func:`default_planner`.
Planner = Callable[[dict[str, str]], str]


class AskError(UmbraError):
    """Raised when a model plan cannot be resolved to a valid, safe search.

    Carries a human- and agent-readable ``message`` (and the offending value
    where useful), so a caller can show the model what to fix.
    """


@dataclass(frozen=True)
class AreaOfInterest:
    """A named polygon the *caller* supplied, which a plan may select by name.

    The unit of the "chosen, never authored" rule described in the module
    docstring: ``geometry`` is already-parsed exterior rings (whatever
    :func:`umbra_py._geometry.parse_geometry` accepted), so the coordinates come
    from the user's own file and the model contributes only the ``name``.

    ``source`` is how the user spelled it on the command line (a path, or inline
    GeoJSON), kept so the audited ``umbra search --intersects …`` line is the
    command they would have typed rather than an inlined ring dump.
    """

    name: str
    geometry: Geometry
    source: str | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        """The area's bounding box -- what the prompt shows the model, and what
        makes an ``--json`` plan auditable without the full ring list."""
        return geometry_bbox(self.geometry)

    def to_dict(self) -> dict[str, Any]:
        """A JSON view for ``--json``: the name, the spelling, and the bounds.

        The rings themselves are deliberately omitted -- they are the user's
        input, unchanged, and can be arbitrarily large; ``source`` says where to
        find them.
        """
        bbox = self.bbox
        return {
            "name": self.name,
            "source": self.source,
            "bbox": list(bbox) if bbox else None,
        }


def _aoi_index(aois: Sequence[AreaOfInterest]) -> dict[str, AreaOfInterest]:
    """Case-insensitive name -> area lookup, for validating a model's choice.

    Names are matched loosely (case and surrounding space) because the model is
    copying a label out of the prompt, not producing data: "Coast" for ``coast``
    is a transcription difference, not a different area. Names are expected to be
    distinct (``umbra ask`` refuses a repeated ``--aoi`` name for this reason);
    a caller that duplicates one anyway gets the last of them.
    """
    return {aoi.name.strip().lower(): aoi for aoi in aois}


@dataclass
class SearchPlan:
    """A validated, deterministic search the model's plan maps to.

    Every field has already passed through :func:`parse_plan` -- dates are ISO
    ``YYYY-MM-DD`` strings, ``product_types`` are canonical
    :data:`umbra_py.PRODUCT_ASSETS` names, ``bbox`` is a 4-tuple of floats.
    ``place`` (a free-text name geocoded at execution time), ``bbox`` and
    ``aoi`` are mutually exclusive -- one spatial filter, however it was spelled.
    ``rationale`` is the model's one-line explanation, kept only to show the
    user; it never becomes a filter.
    """

    question: str
    area: str | None = None
    fuzzy: bool = False
    place: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    aoi: AreaOfInterest | None = None
    start: str | None = None
    end: str | None = None
    product_types: list[str] = field(default_factory=list)
    polarizations: list[str] = field(default_factory=list)
    min_incidence: float | None = None
    max_incidence: float | None = None
    max_resolution: float | None = None
    limit: int | None = None
    max_per_task: int | None = None
    rationale: str | None = None

    def to_search_kwargs(self) -> dict[str, Any]:
        """The keyword arguments for :meth:`umbra_py.UmbraCatalog.search`.

        Omits ``place``/``bbox`` -- the caller resolves those into a single
        ``bbox`` (geocoding ``place``) in the deterministic execution layer,
        exactly as the ``umbra search`` command does. A selected ``aoi`` needs no
        such resolution (its rings were parsed before the model ever saw its
        name), so it passes straight through as ``intersects``. The SAR
        acquisition-property filters (``polarizations`` / ``min_incidence`` /
        ``max_incidence`` / ``max_resolution``) push through to the same
        :meth:`~umbra_py.models.UmbraItem.matches_filters` predicate every other
        surface shares.
        """
        return {
            "intersects": self.aoi.geometry if self.aoi else None,
            "start": self.start,
            "end": self.end,
            "product_types": self.product_types or None,
            "area": self.area,
            "fuzzy": self.fuzzy,
            "polarizations": self.polarizations or None,
            "min_incidence": self.min_incidence,
            "max_incidence": self.max_incidence,
            "max_resolution": self.max_resolution,
            "limit": self.limit,
            "max_per_task": self.max_per_task,
        }

    def to_command(self) -> str:
        """The plan as a copy-pasteable ``umbra search ...`` command string."""
        return plan_to_command(self)

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serialisable view of the plan (for ``--json``).

        Published as ``docs/schemas/search-plan.schema.json`` -- the document a
        caller audits before running the plan, which is why ``command`` is in it.
        """
        return {
            "question": self.question,
            "area": self.area,
            "fuzzy": self.fuzzy,
            "place": self.place,
            "bbox": list(self.bbox) if self.bbox else None,
            "aoi": self.aoi.to_dict() if self.aoi else None,
            "start": self.start,
            "end": self.end,
            "product_types": self.product_types,
            "polarizations": self.polarizations,
            "min_incidence": self.min_incidence,
            "max_incidence": self.max_incidence,
            "max_resolution": self.max_resolution,
            "limit": self.limit,
            "max_per_task": self.max_per_task,
            "rationale": self.rationale,
            "command": plan_to_command(self),
        }


# --- Prompt construction (deterministic) ------------------------------------

#: The exact JSON shape the model must return. Documented in the prompt so the
#: model fills a stable schema; :func:`parse_plan` validates whatever comes back.
#: ``aoi`` is deliberately absent: it is offered only when the caller supplies
#: areas of interest (see :data:`_AOI_PROMPT`), so a model with nothing to choose
#: between is never shown the key.
_PLAN_KEYS = (
    "area",
    "fuzzy",
    "place",
    "bbox",
    "start",
    "end",
    "product_types",
    "polarizations",
    "min_incidence",
    "max_incidence",
    "max_resolution",
    "limit",
    "max_per_task",
    "rationale",
)

_SYSTEM_PROMPT = """\
You translate a user's plain-language request into search parameters for
umbra-py, a toolkit over Umbra's open SAR satellite archive. You do NOT answer
the question or invent data -- you only choose the search filters.

The domain facts you need (product types, the meaning of each search parameter,
the license) are in the JSON context document below. Read it before planning.

Return ONE JSON object and nothing else -- no prose, no code fence. Use exactly
these keys (use null / [] / false when a filter does not apply):

  area          string | null   -- an Umbra task/site name to match (e.g.
                                    "Centerfield, Utah"). Use for a named site.
  fuzzy         boolean          -- true to match `area` loosely (word-order-
                                    and typo-tolerant); prefer true when the
                                    user's site name may be approximate.
  place         string | null    -- a geographic place to geocode to a bbox
                                    (e.g. "Port of Long Beach"). Use `place`
                                    OR `bbox`, never both, and prefer `area`
                                    for a named Umbra site.
  bbox          [w,s,e,n] | null -- an explicit lon/lat box in WGS84 degrees.
  start         string | null    -- earliest date, INCLUSIVE. Emit a concrete
                                    ISO date (YYYY-MM-DD), a bare year/month
                                    (2024, 2024-03), or one of these relative
                                    forms: today, yesterday, "N days/weeks/
                                    months/years ago", "this|last week|month|
                                    year". Resolve seasons yourself to concrete
                                    dates (e.g. northern-hemisphere spring 2024
                                    -> start 2024-03-01, end 2024-05-31).
  end           string | null    -- latest date, INCLUSIVE. Same forms as start.
  product_types array of string  -- subset of the product types in the context
                                    (e.g. ["GEC"]). [] means all.
  polarizations array of string  -- keep only scenes exposing at least one of
                                    these SAR polarizations (VV/VH/HH/HV,
                                    case-insensitive). [] means any. Use this
                                    when the request names a polarization or asks
                                    to keep a comparison like-with-like.
  min_incidence number | null    -- lower bound (inclusive, degrees) on the view
                                    incidence angle. null for no lower bound.
  max_incidence number | null    -- upper bound (inclusive, degrees) on the view
                                    incidence angle. null for no upper bound.
  max_resolution number | null   -- keep only scenes at least this fine: both
                                    range and azimuth resolution <= this many
                                    metres. null for no resolution constraint.
  limit         integer | null   -- max results; null for the tool default.
  max_per_task  integer | null   -- cap per site; 1 gives one pin per site.
  rationale     string           -- one short sentence: how you read the request.

Only choose product types and parameter names that appear in the context.
"""

#: Appended to the system prompt only when the caller supplied areas of
#: interest. It adds one key to the schema -- and states the rule that makes the
#: key safe: the model selects a name from the list, it never writes coordinates.
_AOI_PROMPT = """\
The user supplied these areas of interest. Each is a polygon they already have;
you may select ONE of them by name:

{listing}

  aoi           string | null   -- the exact name of one area of interest from
                                    the list above, when the request refers to a
                                    shape rather than a rectangle ("over this
                                    watershed", "inside the AOI", "along the
                                    coastline I gave you"). Use `aoi` OR `place`
                                    OR `bbox` -- never more than one. Use null
                                    when the request does not point at one of
                                    them.

You cannot describe an area of interest yourself: there is no way to write
coordinates for one, and a name that is not in the list above is rejected. If
none of them fits the request, leave `aoi` null and use `place`/`bbox`/`area`.
"""


def _aoi_listing(aois: Sequence[AreaOfInterest]) -> str:
    """Render the supplied areas as prompt lines: name, part count, bounds.

    The bounds are what let the model tell two supplied areas apart when their
    names are uninformative (``aoi1``/``aoi2``), and they are safe to show --
    they are derived from the user's own file, not something the model can
    edit into the plan.
    """
    lines = []
    for aoi in aois:
        bbox = aoi.bbox
        where = " covering lon/lat " + ", ".join(f"{v:g}" for v in bbox) if bbox else " (empty)"
        parts = f"{len(aoi.geometry)} polygon{'s' if len(aoi.geometry) != 1 else ''}"
        lines.append(f'  - "{aoi.name}" -- {parts},{where}')
    return "\n".join(lines)


def build_messages(question: str, aois: Sequence[AreaOfInterest] = ()) -> dict[str, str]:
    """Build the ``{"system", "user"}`` prompt for a planning model.

    Deterministic and offline: the system prompt embeds the
    :func:`umbra_py.llm_context` domain document and the required JSON schema;
    the user message is the question. This is what an injectable
    :data:`Planner` receives.

    ``aois`` are the caller's own areas of interest (see :class:`AreaOfInterest`).
    When any are supplied, the prompt gains a block listing them by name and the
    ``aoi`` key that selects one; with none supplied the prompt is unchanged, so
    a model is never offered a filter the caller cannot honour.
    """
    context = json.dumps(llm_context(), indent=2)
    system = f"{_SYSTEM_PROMPT}\n\nContext document:\n{context}"
    if aois:
        system += "\n\n" + _AOI_PROMPT.format(listing=_aoi_listing(aois))
    return {"system": system, "user": question.strip()}


# --- Plan parsing & validation (deterministic determinism boundary) ---------


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the single JSON object out of a model reply.

    Tolerates the common wrappers a model adds despite instructions: a
    ```json`` code fence, or leading/trailing prose around the object. Raises
    :class:`AskError` if no JSON object can be parsed.
    """
    stripped = text.strip()
    # Strip a leading/trailing Markdown code fence if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        # Fall back to the first balanced {...} span.
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise AskError(
                f"The model reply did not contain a JSON object. Got: {text[:200]!r}"
            ) from None
        try:
            obj = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AskError(f"Could not parse the model's JSON plan: {exc}") from exc
    if not isinstance(obj, dict):
        raise AskError(f"Expected a JSON object from the model, got {type(obj).__name__}.")
    return obj


def _coerce_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if value in (None, "", []):
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise AskError(f"bbox must be [min_lon, min_lat, max_lon, max_lat], got {value!r}.")
    try:
        w, s, e, n = (float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise AskError(f"bbox values must be numbers, got {value!r}.") from exc
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90):
        raise AskError(f"bbox is out of WGS84 range: {value!r}.")
    if w > e or s > n:
        raise AskError(f"bbox min must not exceed max: {value!r}.")
    return (w, s, e, n)


def _coerce_products(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise AskError(f"product_types must be a list, got {value!r}.")
    canonical = {p.upper(): p for p in PRODUCT_ASSETS}
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AskError(f"product_types entries must be strings, got {item!r}.")
        key = item.strip().upper()
        if key not in canonical:
            raise AskError(
                f"Unknown product type {item!r}. Valid types: {', '.join(PRODUCT_ASSETS)}."
            )
        if canonical[key] not in out:
            out.append(canonical[key])
    return out


def _coerce_polarizations(value: Any) -> list[str]:
    """Normalise a model-emitted ``polarizations`` value into an upper-cased list.

    Accepts a single string or a list/tuple of strings; each entry is
    upper-cased and de-duplicated. SAR polarizations are a small open set
    (``VV``/``VH``/``HH``/``HV``) with no fixed vocabulary to validate against --
    a value the archive never carries simply matches nothing, exactly as
    :func:`umbra_py.serve.parse_polarizations` and
    :meth:`umbra_py.models.UmbraItem.matches_filters` treat it. Returns ``[]`` for
    empty input.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise AskError(f"polarizations must be a list, got {value!r}.")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AskError(f"polarizations entries must be strings, got {item!r}.")
        pol = item.strip().upper()
        if pol and pol not in out:
            out.append(pol)
    return out


def _coerce_positive_float(value: Any, field_name: str) -> float | None:
    """Coerce an optional numeric filter (incidence/resolution) to a positive float.

    Mirrors :func:`_coerce_positive_int`: an empty value is no constraint
    (``None``), a non-number or a non-positive number is a self-describing
    :class:`AskError` -- so a hallucinated ``max_resolution: 0`` is caught at the
    determinism boundary, not passed to the backend.
    """
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise AskError(f"{field_name} must be a number, got {value!r}.") from exc
    if n <= 0:
        raise AskError(f"{field_name} must be positive, got {n}.")
    return n


def _coerce_aoi(value: Any, aois: Sequence[AreaOfInterest]) -> AreaOfInterest | None:
    """Resolve a model-chosen ``aoi`` name to one of the caller's own areas.

    The whole determinism argument for polygon planning lives in these few
    lines: the return value is an object built from the *user's* file, selected
    by name, so no coordinate the model produced can reach a search. A name that
    is not on the list -- including any name at all when the caller supplied no
    areas -- is a self-describing :class:`AskError` rather than a dropped filter,
    because silently searching the whole world for "over this watershed" is the
    one failure mode a polygon filter exists to prevent.
    """
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AskError(f"aoi must be the name of a supplied area of interest, got {value!r}.")
    if not aois:
        raise AskError(
            f"The plan selected the area of interest {value!r}, but none were supplied. "
            "Pass one with --aoi (a .geojson path), or plan a place/bbox instead."
        )
    chosen = _aoi_index(aois).get(value.strip().lower())
    if chosen is None:
        names = ", ".join(repr(a.name) for a in aois)
        raise AskError(f"Unknown area of interest {value!r}. Supplied areas: {names}.")
    return chosen


def _coerce_positive_int(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise AskError(f"{field_name} must be an integer, got {value!r}.") from exc
    if n <= 0:
        raise AskError(f"{field_name} must be positive, got {n}.")
    return n


def _coerce_date_field(value: Any, *, is_end: bool, today: date | None) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AskError(f"Date must be a string, got {value!r}.")
    try:
        resolved = parse_date_bound(value, is_end=is_end, today=today)
    except ValueError as exc:
        # The deterministic layer rejected it -- surface the self-describing
        # message so the model (or user) can emit a concrete date instead.
        raise AskError(str(exc)) from exc
    return resolved.isoformat() if resolved else None


def parse_plan(
    raw: dict[str, Any],
    question: str,
    *,
    today: date | None = None,
    aois: Sequence[AreaOfInterest] = (),
) -> SearchPlan:
    """Validate a model's raw plan dict into a :class:`SearchPlan`.

    **This is the determinism boundary.** Every field the model produced is
    re-checked here before it can become a filter: dates are resolved by
    :func:`umbra_py.parse_date_bound` (so a season or a bad date is caught),
    product types must be canonical :data:`umbra_py.PRODUCT_ASSETS`, the bbox is
    range-checked, and ``place``/``bbox``/``aoi`` are enforced mutually
    exclusive. Unknown keys are ignored. Raises :class:`AskError` with a
    self-describing message on any invalid field.

    ``aois`` are the caller's areas of interest; a plan's ``aoi`` must name one
    of them (see :func:`_coerce_aoi`), so the polygon a search runs against is
    always the user's own geometry.

    ``today`` anchors relative dates for deterministic tests, mirroring
    :func:`umbra_py.parse_date_bound`.
    """
    if not isinstance(raw, dict):
        raise AskError(f"Expected a JSON object plan, got {type(raw).__name__}.")

    area = raw.get("area") or None
    if area is not None and not isinstance(area, str):
        raise AskError(f"area must be a string, got {area!r}.")

    place = raw.get("place") or None
    if place is not None and not isinstance(place, str):
        raise AskError(f"place must be a string, got {place!r}.")

    bbox = _coerce_bbox(raw.get("bbox"))
    if place and bbox:
        raise AskError("A plan may set place or bbox, not both.")

    aoi = _coerce_aoi(raw.get("aoi"), aois)
    if aoi and (place or bbox):
        raise AskError("A plan may set aoi, place or bbox -- not more than one.")

    min_incidence = _coerce_positive_float(raw.get("min_incidence"), "min_incidence")
    max_incidence = _coerce_positive_float(raw.get("max_incidence"), "max_incidence")
    if min_incidence is not None and max_incidence is not None and min_incidence > max_incidence:
        raise AskError(
            f"min_incidence {min_incidence:g} is greater than max_incidence {max_incidence:g}."
        )

    plan = SearchPlan(
        question=question,
        area=area.strip() if area else None,
        fuzzy=bool(raw.get("fuzzy", False)),
        place=place.strip() if place else None,
        bbox=bbox,
        aoi=aoi,
        start=_coerce_date_field(raw.get("start"), is_end=False, today=today),
        end=_coerce_date_field(raw.get("end"), is_end=True, today=today),
        product_types=_coerce_products(raw.get("product_types")),
        polarizations=_coerce_polarizations(raw.get("polarizations")),
        min_incidence=min_incidence,
        max_incidence=max_incidence,
        max_resolution=_coerce_positive_float(raw.get("max_resolution"), "max_resolution"),
        limit=_coerce_positive_int(raw.get("limit"), "limit"),
        max_per_task=_coerce_positive_int(raw.get("max_per_task"), "max_per_task"),
        rationale=(str(raw["rationale"]) if raw.get("rationale") else None),
    )
    if plan.start and plan.end and plan.start > plan.end:
        raise AskError(f"start {plan.start} is after end {plan.end}.")
    return plan


# --- Rendering the deterministic command (for the audit step) ---------------


def plan_to_argv(plan: SearchPlan) -> list[str]:
    """Render the plan as ``umbra search`` argv (without the ``umbra`` prefix).

    The exact deterministic command the plan will run, so the user can audit it
    before it executes -- and copy/paste or tweak it by hand.
    """
    argv: list[str] = ["search"]
    if plan.area:
        argv += ["--area", plan.area]
    if plan.fuzzy:
        argv.append("--fuzzy")
    if plan.place:
        argv += ["--place", plan.place]
    if plan.bbox:
        argv += ["--bbox", ",".join(f"{v:g}" for v in plan.bbox)]
    if plan.aoi:
        # Prefer the spelling the user gave (a path), so the audit line is the
        # command they would have typed; fall back to inline GeoJSON for an area
        # constructed in code, which keeps the rendered command runnable.
        argv += ["--intersects", plan.aoi.source or json.dumps(to_geojson(plan.aoi.geometry))]
    if plan.start:
        argv += ["--start", plan.start]
    if plan.end:
        argv += ["--end", plan.end]
    for product in plan.product_types:
        argv += ["--product", product]
    for pol in plan.polarizations:
        argv += ["--pol", pol]
    if plan.min_incidence is not None:
        argv += ["--min-incidence", f"{plan.min_incidence:g}"]
    if plan.max_incidence is not None:
        argv += ["--max-incidence", f"{plan.max_incidence:g}"]
    if plan.max_resolution is not None:
        argv += ["--max-resolution", f"{plan.max_resolution:g}"]
    if plan.limit is not None:
        argv += ["--limit", str(plan.limit)]
    if plan.max_per_task is not None:
        argv += ["--max-per-task", str(plan.max_per_task)]
    return argv


def plan_to_command(plan: SearchPlan) -> str:
    """The plan as a copy-pasteable ``umbra search ...`` command string."""
    import shlex

    return "umbra " + shlex.join(plan_to_argv(plan))


# --- The model boundary (the only part that calls a model) ------------------


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    import requests  # a core dependency; imported here to keep the module light

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code >= 400:
        raise AskError(f"The model endpoint returned HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _anthropic_planner(*, api_key: str, model: str, base_url: str) -> Planner:
    def planner(messages: dict[str, str]) -> str:
        data = _post_json(
            f"{base_url.rstrip('/')}/v1/messages",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model,
                "max_tokens": 1024,
                "system": messages["system"],
                "messages": [{"role": "user", "content": messages["user"]}],
            },
        )
        try:
            return "".join(
                block.get("text", "") for block in data["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise AskError(f"Unexpected Anthropic response shape: {exc}") from exc

    return planner


def _openai_planner(
    *, api_key: str, model: str, base_url: str, extra_headers: dict[str, str] | None = None
) -> Planner:
    def planner(messages: dict[str, str]) -> str:
        headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        data = _post_json(
            f"{base_url.rstrip('/')}/chat/completions",
            headers,
            {
                "model": model,
                "temperature": 0,
                # Bound the completion, matching the Anthropic path's 1024: a
                # search plan is a small JSON object, and an omitted bound makes a
                # gateway like OpenRouter reserve the model's full output budget
                # against the key's credit limit and 402 the request.
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": messages["system"]},
                    {"role": "user", "content": messages["user"]},
                ],
            },
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AskError(f"Unexpected OpenAI response shape: {exc}") from exc

    return planner


def default_planner(*, model: str | None = None) -> Planner:
    """Build a :data:`Planner` from environment variables.

    Chooses a provider by which key is set, so ``umbra ask`` works against any
    of them with no code change:

    - ``ANTHROPIC_API_KEY`` -> Anthropic Messages API (``ANTHROPIC_BASE_URL``
      overrides the host; model default ``claude-sonnet-5``).
    - else ``OPENROUTER_API_KEY`` -> OpenRouter's OpenAI-compatible endpoint
      (``OPENROUTER_BASE_URL`` overrides the host; model default
      ``openai/gpt-4o-mini``). Checked before ``OPENAI_API_KEY`` because it is an
      unambiguous opt-in, so it wins over a stray ``OPENAI_API_KEY``.
    - else ``OPENAI_API_KEY`` -> OpenAI-compatible chat completions
      (``OPENAI_BASE_URL`` overrides the host, e.g. a local or proxy endpoint;
      model default ``gpt-4o-mini``).

    ``UMBRA_ASK_MODEL`` (or the ``model=`` argument / ``--model`` flag) overrides
    the model for whichever provider is selected -- name an OpenRouter model like
    ``anthropic/claude-3.5-sonnet`` to pick one there. Raises
    :class:`umbra_py.MissingDependencyError` with setup guidance when no key is
    configured -- the feature never runs without an explicit, user-supplied key.
    """
    model = model or os.environ.get("UMBRA_ASK_MODEL")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _anthropic_planner(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=model or "claude-sonnet-5",
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
    if os.environ.get("OPENROUTER_API_KEY"):
        return _openai_planner(
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=model or OPENROUTER_DEFAULT_MODEL,
            base_url=os.environ.get("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
            extra_headers=OPENROUTER_HEADERS,
        )
    if os.environ.get("OPENAI_API_KEY"):
        return _openai_planner(
            api_key=os.environ["OPENAI_API_KEY"],
            model=model or "gpt-4o-mini",
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    raise MissingDependencyError(
        "umbra ask needs a model API key. Set ANTHROPIC_API_KEY, "
        "OPENROUTER_API_KEY (for OpenRouter), or OPENAI_API_KEY (optionally with "
        "OPENAI_BASE_URL for another compatible endpoint) and, optionally, "
        "UMBRA_ASK_MODEL to pick the model. The model only plans the search; the "
        "library still runs it deterministically.",
        hint="Set ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or OPENAI_API_KEY",
    )


def ask(
    question: str,
    *,
    planner: Planner | None = None,
    model: str | None = None,
    today: date | None = None,
    aois: Sequence[AreaOfInterest] = (),
) -> SearchPlan:
    """Turn a natural-language ``question`` into a validated :class:`SearchPlan`.

    Builds the prompt (:func:`build_messages`), calls the ``planner`` (default:
    :func:`default_planner`, chosen from environment keys) to get the model's
    raw reply, then validates it deterministically (:func:`parse_plan`). The
    returned plan is safe to execute: every filter has passed the determinism
    boundary. The model is *only* consulted to produce the raw plan; inject a
    ``planner`` in tests to avoid any network call.

    ``aois`` offers the model the caller's own areas of interest to choose
    between (``umbra ask --aoi coast.geojson``). The same sequence goes into the
    prompt and into the validation, so a plan can only ever select one of them.
    """
    if not question or not question.strip():
        raise AskError('Ask a question, e.g. "what changed at Centerfield this spring?"')
    plan_fn = planner or default_planner(model=model)
    reply = plan_fn(build_messages(question, aois))
    raw = _extract_json_object(reply)
    return parse_plan(raw, question, today=today, aois=aois)
