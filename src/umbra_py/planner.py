"""``umbra ask``: natural-language search that a model *plans* and the library
*executes* deterministically.

This is the capstone of the natural-language-search direction in
``docs/AI_INTEGRATION_IDEAS.md`` (C1). The two earlier steps stayed entirely
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
   is range-checked. **Nothing the model says becomes a filter without passing
   through this validation**, so a hallucinated date or product type is an error,
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
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .constants import PRODUCT_ASSETS
from .context import llm_context
from .dates import parse_date_bound
from .exceptions import MissingDependencyError, UmbraError

__all__ = [
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


@dataclass
class SearchPlan:
    """A validated, deterministic search the model's plan maps to.

    Every field has already passed through :func:`parse_plan` -- dates are ISO
    ``YYYY-MM-DD`` strings, ``product_types`` are canonical
    :data:`umbra_py.PRODUCT_ASSETS` names, ``bbox`` is a 4-tuple of floats.
    ``place`` (a free-text name geocoded at execution time) and ``bbox`` are
    mutually exclusive. ``rationale`` is the model's one-line explanation, kept
    only to show the user; it never becomes a filter.
    """

    question: str
    area: str | None = None
    fuzzy: bool = False
    place: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    start: str | None = None
    end: str | None = None
    product_types: list[str] = field(default_factory=list)
    limit: int | None = None
    max_per_task: int | None = None
    rationale: str | None = None

    def to_search_kwargs(self) -> dict[str, Any]:
        """The keyword arguments for :meth:`umbra_py.UmbraCatalog.search`.

        Omits ``place``/``bbox`` -- the caller resolves those into a single
        ``bbox`` (geocoding ``place``) in the deterministic execution layer,
        exactly as the ``umbra search`` command does.
        """
        return {
            "start": self.start,
            "end": self.end,
            "product_types": self.product_types or None,
            "area": self.area,
            "fuzzy": self.fuzzy,
            "limit": self.limit,
            "max_per_task": self.max_per_task,
        }

    def to_command(self) -> str:
        """The plan as a copy-pasteable ``umbra search ...`` command string."""
        return plan_to_command(self)

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serialisable view of the plan (for ``--json``)."""
        return {
            "question": self.question,
            "area": self.area,
            "fuzzy": self.fuzzy,
            "place": self.place,
            "bbox": list(self.bbox) if self.bbox else None,
            "start": self.start,
            "end": self.end,
            "product_types": self.product_types,
            "limit": self.limit,
            "max_per_task": self.max_per_task,
            "rationale": self.rationale,
            "command": plan_to_command(self),
        }


# --- Prompt construction (deterministic) ------------------------------------

#: The exact JSON shape the model must return. Documented in the prompt so the
#: model fills a stable schema; :func:`parse_plan` validates whatever comes back.
_PLAN_KEYS = (
    "area",
    "fuzzy",
    "place",
    "bbox",
    "start",
    "end",
    "product_types",
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
  limit         integer | null   -- max results; null for the tool default.
  max_per_task  integer | null   -- cap per site; 1 gives one pin per site.
  rationale     string           -- one short sentence: how you read the request.

Only choose product types and parameter names that appear in the context.
"""


def build_messages(question: str) -> dict[str, str]:
    """Build the ``{"system", "user"}`` prompt for a planning model.

    Deterministic and offline: the system prompt embeds the
    :func:`umbra_py.llm_context` domain document and the required JSON schema;
    the user message is the question. This is what an injectable
    :data:`Planner` receives.
    """
    context = json.dumps(llm_context(), indent=2)
    system = f"{_SYSTEM_PROMPT}\n\nContext document:\n{context}"
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


def parse_plan(raw: dict[str, Any], question: str, *, today: date | None = None) -> SearchPlan:
    """Validate a model's raw plan dict into a :class:`SearchPlan`.

    **This is the determinism boundary.** Every field the model produced is
    re-checked here before it can become a filter: dates are resolved by
    :func:`umbra_py.parse_date_bound` (so a season or a bad date is caught),
    product types must be canonical :data:`umbra_py.PRODUCT_ASSETS`, the bbox is
    range-checked, and ``place``/``bbox`` are enforced mutually exclusive.
    Unknown keys are ignored. Raises :class:`AskError` with a self-describing
    message on any invalid field.

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

    plan = SearchPlan(
        question=question,
        area=area.strip() if area else None,
        fuzzy=bool(raw.get("fuzzy", False)),
        place=place.strip() if place else None,
        bbox=bbox,
        start=_coerce_date_field(raw.get("start"), is_end=False, today=today),
        end=_coerce_date_field(raw.get("end"), is_end=True, today=today),
        product_types=_coerce_products(raw.get("product_types")),
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
    if plan.start:
        argv += ["--start", plan.start]
    if plan.end:
        argv += ["--end", plan.end]
    for product in plan.product_types:
        argv += ["--product", product]
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


def _openai_planner(*, api_key: str, model: str, base_url: str) -> Planner:
    def planner(messages: dict[str, str]) -> str:
        data = _post_json(
            f"{base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            {
                "model": model,
                "temperature": 0,
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
    - else ``OPENAI_API_KEY`` -> OpenAI-compatible chat completions
      (``OPENAI_BASE_URL`` overrides the host, e.g. a local or proxy endpoint;
      model default ``gpt-4o-mini``).

    ``UMBRA_ASK_MODEL`` (or the ``model=`` argument / ``--model`` flag) overrides
    the model for whichever provider is selected. Raises
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
    if os.environ.get("OPENAI_API_KEY"):
        return _openai_planner(
            api_key=os.environ["OPENAI_API_KEY"],
            model=model or "gpt-4o-mini",
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    raise MissingDependencyError(
        "umbra ask needs a model API key. Set ANTHROPIC_API_KEY (or "
        "OPENAI_API_KEY, optionally with OPENAI_BASE_URL for a compatible "
        "endpoint) and, optionally, UMBRA_ASK_MODEL to pick the model. "
        "The model only plans the search; the library still runs it "
        "deterministically.",
        hint="Set ANTHROPIC_API_KEY (or OPENAI_API_KEY)",
    )


def ask(
    question: str,
    *,
    planner: Planner | None = None,
    model: str | None = None,
    today: date | None = None,
) -> SearchPlan:
    """Turn a natural-language ``question`` into a validated :class:`SearchPlan`.

    Builds the prompt (:func:`build_messages`), calls the ``planner`` (default:
    :func:`default_planner`, chosen from environment keys) to get the model's
    raw reply, then validates it deterministically (:func:`parse_plan`). The
    returned plan is safe to execute: every filter has passed the determinism
    boundary. The model is *only* consulted to produce the raw plan; inject a
    ``planner`` in tests to avoid any network call.
    """
    if not question or not question.strip():
        raise AskError('Ask a question, e.g. "what changed at Centerfield this spring?"')
    plan_fn = planner or default_planner(model=model)
    reply = plan_fn(build_messages(question))
    raw = _extract_json_object(reply)
    return parse_plan(raw, question, today=today)
