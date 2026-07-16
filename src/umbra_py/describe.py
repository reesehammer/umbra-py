"""``umbra describe``: a vision-language reading of a SAR scene, grounded in the
imagery the library already renders and the domain facts it already carries.

This is the first of the Tier C "VLM-in-the-loop" capabilities in
``docs/AI_INTEGRATION_IDEAS.md`` (C2). Where ``umbra ask``
(:mod:`umbra_py.planner`) lets a model *plan* a search, ``umbra describe`` lets a
model *read* a scene: it renders an item's quicklook, sends that picture plus the
item's :meth:`~umbra_py.UmbraItem.to_llm_context` card to a vision model, and
returns a structured description -- ``{summary, observed_features[], confidence,
caveats[]}``. The idea is the one the whole AI direction rests on: the library's
outputs are *images with precise metadata*, the native input of a VLM, so nothing
new has to be invented -- only connected.

How it stays honest
-------------------
The library's determinism boundary (``docs/AI_INTEGRATION_IDEAS.md`` §A4, §6.1)
still holds:

1. **The picture and the metadata are produced deterministically.** The quicklook
   is the same :func:`umbra_py.quicklook` render every other command uses; the
   context card is the same offline :meth:`~umbra_py.UmbraItem.to_llm_context`.
   The model is shown facts, not asked to invent them.
2. **The model only interprets; it never becomes a filter, a URL, or a
   coordinate.** Its reply is validated into a :class:`SceneDescription` by the
   deterministic :func:`parse_description` -- a description is text *about* the
   scene, never data the rest of the library acts on.
3. **Provenance is mandatory.** Every :class:`SceneDescription` carries the
   CC-BY :data:`~umbra_py.constants.ATTRIBUTION` and the
   :data:`~umbra_py.constants.AI_PROVENANCE` note, so a downstream reader can
   never mistake a model's reading of radar for a measurement. The same license
   discipline the library applies to GeoTIFF tags, extended to model text.

Like :mod:`umbra_py.planner` and :mod:`umbra_py.semantic`, the model call is an
injectable :data:`Describer` callable and the render step is an injectable
:data:`Renderer`, so the deterministic pieces (:func:`build_describe_messages`,
:func:`parse_description`) are stdlib-only and fully offline-testable and no test
ever touches the network. The default describer is built from environment
variables and uses only :mod:`requests` (a core dependency) -- no heavy SDK. The
whole feature lives behind the ``[ai]`` extra (plus ``[viz]`` for the render) and
never runs implicitly: only ``umbra describe`` reaches a model, and only when the
user invokes it with a key configured.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .constants import AI_PROVENANCE, ATTRIBUTION, POLARIZATION_CAVEAT
from .exceptions import MissingDependencyError, UmbraError
from .models import UmbraItem

__all__ = [
    "DescribeError",
    "SceneDescription",
    "Describer",
    "Renderer",
    "build_describe_messages",
    "parse_description",
    "render_quicklook_png",
    "default_describer",
    "describe",
]

#: A describer turns the multimodal prompt (``{"system": str, "user": str,
#: "image_png": bytes}``) into the model's raw text reply. Injectable so tests
#: never call a model; the default implementation is :func:`default_describer`.
Describer = Callable[[dict[str, Any]], str]

#: A renderer turns an item into a PNG quicklook (bytes). Injectable so tests
#: never touch the network or need the ``viz`` extra; the default implementation
#: is :func:`render_quicklook_png`.
Renderer = Callable[[UmbraItem], bytes]


class DescribeError(UmbraError):
    """Raised when a scene cannot be rendered or a model reply cannot be parsed.

    Carries a human- and agent-readable ``message`` so a caller can show what
    went wrong (an unrenderable asset, an unparseable reply).
    """


@dataclass
class SceneDescription:
    """A validated, provenance-stamped model reading of a SAR scene.

    Every field has passed through :func:`parse_description`. ``summary`` is a
    short plain-language paragraph; ``observed_features`` and ``caveats`` are
    lists of short strings; ``confidence`` is the model's own hedge
    (``"low"``/``"medium"``/``"high"`` or ``None``). ``attribution`` and
    ``provenance`` are filled in deterministically -- the model never sets them --
    so the mandatory CC-BY line and the "this is an AI interpretation" note always
    travel with the text.
    """

    item_id: str | None
    summary: str
    observed_features: list[str] = field(default_factory=list)
    confidence: str | None = None
    caveats: list[str] = field(default_factory=list)
    model: str | None = None
    asset: str = "GEC"
    attribution: str = ATTRIBUTION
    provenance: str = AI_PROVENANCE

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serialisable view of the description (for ``--json``)."""
        return {
            "item_id": self.item_id,
            "summary": self.summary,
            "observed_features": self.observed_features,
            "confidence": self.confidence,
            "caveats": self.caveats,
            "model": self.model,
            "asset": self.asset,
            "attribution": self.attribution,
            "provenance": self.provenance,
        }

    def to_text(self) -> str:
        """A human-readable rendering for the CLI's default (non-JSON) output."""
        lines = [self.summary.strip()]
        if self.observed_features:
            lines.append("")
            lines.append("Observed features:")
            lines.extend(f"  - {feat}" for feat in self.observed_features)
        if self.caveats:
            lines.append("")
            lines.append("Caveats:")
            lines.extend(f"  - {cav}" for cav in self.caveats)
        if self.confidence:
            lines.append("")
            lines.append(f"Confidence: {self.confidence}")
        lines.append("")
        lines.append(self.provenance)
        lines.append(self.attribution)
        return "\n".join(lines)


# --- Prompt construction (deterministic) ------------------------------------

#: The SAR literacy a general vision model does not reliably have. Encoded once,
#: in the packaged prompt, where it benefits every user -- so the model reads the
#: radar correctly (bright != hot, dark != empty) rather than as an optical photo.
_SAR_PRIMER = """\
You are reading a Synthetic Aperture Radar (SAR) quicklook, NOT an optical photo.
SAR imaging rules you must apply:

- Brightness is radar backscatter, not sunlight or temperature. Bright pixels are
  strong reflectors (metal, buildings, ships, rough surfaces, slopes facing the
  sensor); dark pixels are smooth surfaces that reflect radar away (calm water,
  roads, dry lakebeds, tarmac).
- Speckle: the grainy salt-and-pepper texture is inherent to SAR, not real
  small-scale structure. Do not describe individual specks as objects.
- Layover and shadow: tall objects (towers, ridges) lay over toward the sensor as
  bright streaks and cast radar shadows (fully dark) behind them. A dark region
  next to a bright one may be shadow, not an empty field.
- Geometry: the scene is geocoded and north-up, but distances/areas are
  approximate at quicklook resolution.
- One image is one moment. Do not claim motion, change, or activity over time from
  a single frame -- only what is visible now.
"""

_SYSTEM_PROMPT = """\
You are a SAR image analyst. You are shown one geocoded SAR quicklook of an Umbra
open-data acquisition and a JSON metadata card for it. Describe ONLY what is
visible in the image, read through the SAR rules below. Do not speculate about
purpose, ownership, or events you cannot see, and do not invent coordinates,
dates, or measurements -- the metadata card already carries the ground truth for
those.

{primer}

Return ONE JSON object and nothing else -- no prose, no code fence. Use exactly
these keys:

  summary            string        -- 2-4 sentences: the dominant land cover /
                                       structures / water and the overall scene,
                                       in plain language a non-specialist reads.
  observed_features  array[string] -- short concrete phrases for distinct things
                                       you can see (e.g. "bright grid of buildings
                                       in the northeast", "dark smooth river
                                       curving south"). [] if none stand out.
  confidence         string        -- your overall confidence the reading is
                                       correct: "low", "medium", or "high".
  caveats            array[string] -- SAR-specific cautions relevant to THIS scene
                                       (e.g. a dark area that could be shadow or
                                       water, speckle in a low-backscatter field).

Keep it grounded: if the scene is largely featureless SAR speckle, say so plainly
rather than inventing structure.
"""


def build_describe_messages(context_card: dict[str, Any], image_png: bytes) -> dict[str, Any]:
    """Build the ``{"system", "user", "image_png"}`` prompt for a vision model.

    Deterministic and offline: the system prompt embeds the SAR primer and the
    required JSON schema; the user message is the item's
    :meth:`~umbra_py.UmbraItem.to_llm_context` card as JSON (so the model reads
    the metadata rather than guessing it), and ``image_png`` is the rendered
    quicklook the model looks at. This is what an injectable :data:`Describer`
    receives.
    """
    system = _SYSTEM_PROMPT.format(primer=_SAR_PRIMER)
    card = json.dumps(context_card, indent=2)
    user = (
        "Metadata card for the scene you are shown (ground truth -- do not "
        f"contradict it):\n{card}\n\nDescribe the SAR quicklook."
    )
    return {"system": system, "user": user, "image_png": image_png}


# --- Reply parsing & validation (the interpretation boundary) ---------------


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the single JSON object out of a model reply.

    Tolerates the wrappers a model adds despite instructions (a ```json`` fence,
    leading/trailing prose). Raises :class:`DescribeError` if none can be parsed.
    """
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise DescribeError(
                f"The model reply did not contain a JSON object. Got: {text[:200]!r}"
            ) from None
        try:
            obj = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise DescribeError(f"Could not parse the model's JSON reply: {exc}") from exc
    if not isinstance(obj, dict):
        raise DescribeError(f"Expected a JSON object from the model, got {type(obj).__name__}.")
    return obj


def _coerce_str_list(value: Any, field_name: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise DescribeError(f"{field_name} must be a list of strings, got {value!r}.")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise DescribeError(f"{field_name} entries must be strings, got {entry!r}.")
        text = entry.strip()
        if text:
            out.append(text)
    return out


_CONFIDENCE_LEVELS = ("low", "medium", "high")


def parse_description(
    raw: dict[str, Any],
    *,
    item_id: str | None = None,
    model: str | None = None,
    asset: str = "GEC",
) -> SceneDescription:
    """Validate a model's raw reply into a :class:`SceneDescription`.

    **This is the interpretation boundary.** The model's text is checked into a
    fixed shape -- ``summary`` must be a non-empty string, the two lists are
    coerced to clean string lists, ``confidence`` is normalised to
    ``low``/``medium``/``high`` (or dropped) -- and the mandatory
    :data:`~umbra_py.constants.ATTRIBUTION` and
    :data:`~umbra_py.constants.AI_PROVENANCE` are stamped on deterministically,
    never taken from the model. Unknown keys are ignored. Raises
    :class:`DescribeError` on a missing or ill-typed ``summary``.
    """
    if not isinstance(raw, dict):
        raise DescribeError(f"Expected a JSON object reply, got {type(raw).__name__}.")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise DescribeError("The model reply is missing a non-empty 'summary' string.")

    confidence = raw.get("confidence")
    if isinstance(confidence, str):
        confidence = confidence.strip().lower() or None
        if confidence is not None and confidence not in _CONFIDENCE_LEVELS:
            # Keep the boundary strict but forgiving: an off-menu word is dropped
            # rather than trusted, so a downstream reader never sees a bogus level.
            confidence = None
    else:
        confidence = None

    return SceneDescription(
        item_id=item_id,
        summary=summary.strip(),
        observed_features=_coerce_str_list(raw.get("observed_features"), "observed_features"),
        confidence=confidence,
        caveats=_coerce_str_list(raw.get("caveats"), "caveats"),
        model=model,
        asset=asset,
    )


# --- Rendering the scene (deterministic; the picture the model reads) --------


def render_quicklook_png(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 1024,
    db: bool = True,
) -> bytes:
    """Render an item's quicklook and return it as PNG bytes for a vision model.

    A thin wrapper over :func:`umbra_py.quicklook` (so the model sees exactly the
    render a human would) that encodes to PNG in memory rather than to a file.
    ``db=True`` by default -- the decibel stretch is the radiometrically-correct
    SAR look and reveals the terrain/structure a model needs. Requires the
    ``viz`` extra.
    """
    from io import BytesIO

    from .viz import quicklook

    try:
        image = quicklook(item, asset=asset, max_size=max_size, db=db)
    except MissingDependencyError:
        raise
    except UmbraError:
        raise
    except Exception as exc:  # rasterio / PIL surface a variety of read errors
        raise DescribeError(f"Could not render a quicklook of {item.id}: {exc}") from exc
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# --- The model boundary (the only part that calls a model) ------------------


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    import requests  # a core dependency; imported here to keep the module light

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code >= 400:
        raise DescribeError(
            f"The model endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


def _anthropic_describer(*, api_key: str, model: str, base_url: str) -> Describer:
    def describer(messages: dict[str, Any]) -> str:
        b64 = base64.b64encode(messages["image_png"]).decode("ascii")
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
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": messages["user"]},
                        ],
                    }
                ],
            },
        )
        try:
            return "".join(
                block.get("text", "") for block in data["content"] if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise DescribeError(f"Unexpected Anthropic response shape: {exc}") from exc

    return describer


def _openai_describer(*, api_key: str, model: str, base_url: str) -> Describer:
    def describer(messages: dict[str, Any]) -> str:
        b64 = base64.b64encode(messages["image_png"]).decode("ascii")
        data = _post_json(
            f"{base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            {
                "model": model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": messages["system"]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": messages["user"]},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            },
                        ],
                    },
                ],
            },
        )
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise DescribeError(f"Unexpected OpenAI response shape: {exc}") from exc

    return describer


def default_describer(*, model: str | None = None) -> Describer:
    """Build a :data:`Describer` from environment variables.

    Chooses a provider by which key is set, mirroring
    :func:`umbra_py.planner.default_planner`:

    - ``ANTHROPIC_API_KEY`` -> Anthropic Messages API (``ANTHROPIC_BASE_URL``
      overrides the host; model default ``claude-sonnet-5``).
    - else ``OPENAI_API_KEY`` -> OpenAI-compatible chat completions
      (``OPENAI_BASE_URL`` overrides the host; model default ``gpt-4o-mini``).

    Both defaults are vision-capable. ``UMBRA_DESCRIBE_MODEL`` (or the ``model=``
    argument / ``--model`` flag) overrides the model for whichever provider is
    selected. Raises :class:`umbra_py.MissingDependencyError` with setup guidance
    when no key is configured -- the feature never runs without an explicit,
    user-supplied key.
    """
    model = model or os.environ.get("UMBRA_DESCRIBE_MODEL")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _anthropic_describer(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            model=model or "claude-sonnet-5",
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        )
    if os.environ.get("OPENAI_API_KEY"):
        return _openai_describer(
            api_key=os.environ["OPENAI_API_KEY"],
            model=model or "gpt-4o-mini",
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    raise MissingDependencyError(
        "umbra describe needs a vision model API key. Set ANTHROPIC_API_KEY (or "
        "OPENAI_API_KEY, optionally with OPENAI_BASE_URL for a compatible "
        "endpoint) and, optionally, UMBRA_DESCRIBE_MODEL to pick the model. "
        "The model only interprets the imagery; every description is stamped as "
        "an AI interpretation and carries the CC-BY attribution.",
        hint="Set ANTHROPIC_API_KEY (or OPENAI_API_KEY)",
    )


def describe(
    item: UmbraItem,
    *,
    describer: Describer | None = None,
    render: Renderer | None = None,
    model: str | None = None,
    asset: str = "GEC",
    max_size: int = 1024,
    db: bool = True,
) -> SceneDescription:
    """Render an item's quicklook and return a validated :class:`SceneDescription`.

    Renders the scene (:func:`render_quicklook_png` by default, or an injected
    ``render``), builds the multimodal prompt from the picture and the item's
    :meth:`~umbra_py.UmbraItem.to_llm_context` card, calls the ``describer``
    (default: :func:`default_describer`, chosen from environment keys), and
    validates the reply through :func:`parse_description`. The returned
    description carries the mandatory attribution and AI-provenance note.

    The model is *only* consulted to read the rendered scene; inject a
    ``describer`` and/or a ``render`` in tests to avoid any network call or the
    ``viz`` extra.
    """
    render_fn = render or (
        lambda it: render_quicklook_png(it, asset=asset, max_size=max_size, db=db)
    )
    image_png = render_fn(item)

    card = item.to_llm_context()
    # Belt-and-braces: the polarization caveat travels with any change reasoning;
    # keep it in the card the model reads even for a single-scene description.
    card.setdefault("polarization_caveat", POLARIZATION_CAVEAT)

    describe_fn = describer or default_describer(model=model)
    reply = describe_fn(build_describe_messages(card, image_png))
    raw = _extract_json_object(reply)
    return parse_description(raw, item_id=item.id, model=model, asset=asset)
