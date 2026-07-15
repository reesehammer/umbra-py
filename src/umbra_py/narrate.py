"""``umbra change --narrate``: a vision-language reading of *what changed*
between two SAR passes, grounded in a deterministic per-block dB-delta sidecar.

This is the second Tier C "VLM-in-the-loop" capability in
``docs/AI_INTEGRATION_IDEAS.md`` (C2), the sibling of :mod:`umbra_py.describe`.
Where ``umbra describe`` has a model *read one scene*, ``umbra change --narrate``
has a model *narrate the change* between two acquisitions of the same site: it
renders the change composite (the classic green-appeared / magenta-vanished
image), computes a coarse grid of signed backscatter change in decibels, and
sends both the picture and the numbers to a vision model, which returns a
structured :class:`ChangeNarration` -- ``{summary, changes[], confidence,
caveats[]}``.

Why the numeric sidecar matters
-------------------------------
A model shown only the composite can hallucinate change ("a ship appeared in the
harbor") that the pixels do not support. So the narration is grounded in a
deterministic artifact: :func:`compute_change_stats` divides the co-registered
scene into a coarse grid and, per block, measures the mean *signed* change in dB
(``20*log10(later) - 20*log10(earlier)`` -- positive means the block brightened,
negative means it dimmed) plus the fraction of the block that changed beyond a
threshold. The model is handed this grid and told to narrate *only* change the
numbers support, and the same grid ships as a JSON sidecar next to the image --
so every statement in the narration is auditable against a number a human (or a
test) can recompute. Narration cites numbers, not vibes.

How it stays honest
-------------------
The library's determinism boundary (``docs/AI_INTEGRATION_IDEAS.md`` §A4, §6.1)
holds exactly as it does for :mod:`umbra_py.describe`:

1. **The picture and the numbers are produced deterministically.** The composite
   is the same :func:`umbra_py.change_composite` render; the dB grid is plain
   :func:`compute_change_stats`. The model is shown facts, not asked to invent
   them.
2. **The model only interprets.** Its reply is validated into a
   :class:`ChangeNarration` by :func:`parse_narration`; nothing it says becomes a
   filter, a URL, a coordinate, or a measurement -- the measurements are the
   sidecar's, computed offline.
3. **Provenance is mandatory.** Every :class:`ChangeNarration` carries the CC-BY
   :data:`~umbra_py.constants.ATTRIBUTION` and the
   :data:`~umbra_py.constants.AI_PROVENANCE` note, so a downstream reader never
   mistakes a model's reading of radar for ground truth.

Like :mod:`umbra_py.describe`, the model call is an injectable :data:`Narrator`
and the render step an injectable :data:`ChangeRenderer`, so the deterministic
pieces (:func:`compute_change_stats`, :func:`build_narrate_messages`,
:func:`parse_narration`) are stdlib-only and fully offline-testable and no test
ever touches the network. The default narrator reuses the same provider plumbing
as ``umbra describe`` (Anthropic or any OpenAI-compatible endpoint, user-supplied
key, :mod:`requests` only -- no heavy SDK). The whole feature lives behind the
``[ai]`` extra (plus ``[viz]`` for the render) and never runs implicitly: only
``umbra change --narrate`` reaches a model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .constants import AI_PROVENANCE, ATTRIBUTION, POLARIZATION_CAVEAT
from .describe import (
    _SAR_PRIMER,
    _anthropic_describer,
    _coerce_str_list,
    _extract_json_object,
    _openai_describer,
)
from .exceptions import MissingDependencyError, UmbraError
from .models import UmbraItem

__all__ = [
    "NarrateError",
    "ChangeBlock",
    "ChangeStats",
    "ChangeNarration",
    "Narrator",
    "ChangeRenderer",
    "compute_change_stats",
    "build_narrate_messages",
    "parse_narration",
    "render_change_png",
    "save_change_scene",
    "default_narrator",
    "narrate",
]

#: A narrator turns the multimodal prompt (``{"system": str, "user": str,
#: "image_png": bytes}``) into the model's raw text reply. Same contract as
#: :data:`umbra_py.describe.Describer`; injectable so tests never call a model.
Narrator = Callable[[dict[str, Any]], str]

#: A change renderer turns the acquisitions into ``(composite_png, stats)`` -- the
#: PNG the model looks at and the deterministic :class:`ChangeStats` grid it is
#: grounded in. Injectable so tests never touch the network or need the ``viz``
#: extra; the default implementation is :func:`render_change_png`.
ChangeRenderer = Callable[[Sequence[UmbraItem]], "tuple[bytes, ChangeStats]"]


class NarrateError(UmbraError):
    """Raised when a change scene cannot be rendered or a reply cannot be parsed."""


# --- The deterministic sidecar: per-block dB change -------------------------


@dataclass
class ChangeBlock:
    """Signed backscatter change in one cell of the coarse change grid.

    ``row``/``col`` are 0-indexed with ``row=0`` at the north (top) edge.
    ``compass`` is a plain-language location ("northwest", "center", ...).
    ``mean_delta_db`` is the mean *signed* change (positive = brightened in the
    later pass, negative = dimmed); ``mean_abs_delta_db`` is its magnitude.
    ``brightened_fraction`` / ``dimmed_fraction`` are the share of the block's
    valid pixels whose change exceeded the threshold in each direction, and
    ``valid_fraction`` how much of the block was imaged on both passes.
    """

    row: int
    col: int
    compass: str
    mean_delta_db: float | None
    mean_abs_delta_db: float | None
    brightened_fraction: float
    dimmed_fraction: float
    valid_fraction: float


@dataclass
class ChangeStats:
    """A coarse, deterministic grid of backscatter change between two passes.

    This is the auditable artifact the narration is grounded in and the JSON
    sidecar written next to the composite. It is computed by
    :func:`compute_change_stats` from the co-registered dB amplitudes -- no model
    is involved -- so any statement in a :class:`ChangeNarration` can be checked
    against a number here.
    """

    grid_rows: int
    grid_cols: int
    change_threshold_db: float
    bounds: tuple[float, float, float, float]
    blocks: list[ChangeBlock] = field(default_factory=list)
    scene_mean_abs_delta_db: float | None = None
    scene_changed_fraction: float = 0.0
    peak_compass: str | None = None
    peak_direction: str | None = None
    peak_mean_delta_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serialisable view of the grid (for the sidecar / ``--json``)."""
        return {
            "grid_rows": self.grid_rows,
            "grid_cols": self.grid_cols,
            "change_threshold_db": self.change_threshold_db,
            "bounds": list(self.bounds),
            "scene_mean_abs_delta_db": self.scene_mean_abs_delta_db,
            "scene_changed_fraction": self.scene_changed_fraction,
            "peak_compass": self.peak_compass,
            "peak_direction": self.peak_direction,
            "peak_mean_delta_db": self.peak_mean_delta_db,
            "blocks": [asdict(b) for b in self.blocks],
        }

    def to_grid_text(self) -> str:
        """An ASCII heat-grid of signed dB change the model reads spatially.

        Each cell shows the block's mean signed change in dB (``+`` brighter,
        ``-`` dimmer, ``.`` when the block was never imaged on both passes), laid
        out north-up so the model can tie a number to a compass direction.
        """
        by_cell = {(b.row, b.col): b for b in self.blocks}
        lines = []
        for r in range(self.grid_rows):
            cells = []
            for c in range(self.grid_cols):
                block = by_cell.get((r, c))
                if block is None or block.mean_delta_db is None:
                    cells.append("   . ")
                else:
                    cells.append(f"{block.mean_delta_db:+5.1f}")
            lines.append(" ".join(cells))
        return "\n".join(lines)


def _compass_label(row: int, col: int, rows: int, cols: int) -> str:
    """Plain-language location of a grid cell ("northwest", "center", ...)."""

    def band(i: int, n: int) -> int:
        if n <= 1:
            return 1
        t = i / (n - 1)
        if t < 1 / 3:
            return 0
        if t > 2 / 3:
            return 2
        return 1

    ns = ("north", "", "south")[band(row, rows)]
    ew = ("west", "", "east")[band(col, cols)]
    if ns and ew:
        return ns + ew
    return ns or ew or "center"


def compute_change_stats(
    band_earlier: Any,
    band_later: Any,
    bounds: tuple[float, float, float, float],
    *,
    grid: int = 6,
    change_threshold_db: float = 3.0,
) -> ChangeStats:
    """Measure signed backscatter change between two co-registered SAR bands.

    ``band_earlier`` and ``band_later`` are 2D amplitude arrays on the *same*
    pixel grid (co-register first, e.g. with the change composite's own
    reader). The per-pixel signed change is ``20*log10(later) -
    20*log10(earlier)`` in decibels -- positive where the scene brightened in the
    later pass (new/appeared backscatter, the composite's green), negative where
    it dimmed (vanished, the composite's magenta). Pixels non-positive or
    non-finite in *either* band are excluded (they weren't imaged on both
    passes).

    The scene is divided into a ``grid`` x ``grid`` array of blocks and each
    block's mean signed change, change magnitude, and the fraction of it that
    moved past ``change_threshold_db`` in each direction are recorded. The result
    is the deterministic :class:`ChangeStats` a narration is grounded in.

    Pure NumPy and offline: this never fetches anything and is the same whether or
    not a model is ever called. Requires the ``viz`` extra (for NumPy).
    """
    from .viz import _require  # noqa: PLC0415

    np = _require("numpy")

    a = np.asarray(band_earlier, dtype="float64")
    b = np.asarray(band_later, dtype="float64")
    if a.shape != b.shape:
        raise ValueError(f"bands must share a shape; got {a.shape} and {b.shape}.")
    if a.ndim != 2:
        raise ValueError(f"bands must be 2D amplitude arrays; got {a.ndim}D.")
    if grid < 1:
        raise ValueError(f"grid must be >= 1, got {grid}.")

    valid = np.isfinite(a) & np.isfinite(b) & (a > 0) & (b > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(valid, 20.0 * np.log10(b) - 20.0 * np.log10(a), np.nan)

    rows, cols = a.shape
    # Even splits of the pixel grid; np.array_split tolerates non-divisible sizes.
    row_slices = _split_slices(rows, grid)
    col_slices = _split_slices(cols, grid)

    left, bottom, right, top = bounds
    blocks: list[ChangeBlock] = []
    total_valid = 0
    total_changed = 0
    weighted_abs = 0.0
    peak: ChangeBlock | None = None

    for r, rsl in enumerate(row_slices):
        for c, csl in enumerate(col_slices):
            cell = delta[rsl, csl]
            cell_valid = valid[rsl, csl]
            n_pixels = cell.size
            n_valid = int(cell_valid.sum())
            valid_fraction = n_valid / n_pixels if n_pixels else 0.0
            if n_valid == 0:
                blocks.append(
                    ChangeBlock(
                        row=r,
                        col=c,
                        compass=_compass_label(r, c, grid, grid),
                        mean_delta_db=None,
                        mean_abs_delta_db=None,
                        brightened_fraction=0.0,
                        dimmed_fraction=0.0,
                        valid_fraction=0.0,
                    )
                )
                continue
            vals = cell[cell_valid]
            mean_delta = float(np.mean(vals))
            mean_abs = float(np.mean(np.abs(vals)))
            brightened = int(np.count_nonzero(vals > change_threshold_db))
            dimmed = int(np.count_nonzero(vals < -change_threshold_db))
            block = ChangeBlock(
                row=r,
                col=c,
                compass=_compass_label(r, c, grid, grid),
                mean_delta_db=round(mean_delta, 2),
                mean_abs_delta_db=round(mean_abs, 2),
                brightened_fraction=round(brightened / n_valid, 3),
                dimmed_fraction=round(dimmed / n_valid, 3),
                valid_fraction=round(valid_fraction, 3),
            )
            blocks.append(block)
            total_valid += n_valid
            total_changed += brightened + dimmed
            weighted_abs += mean_abs * n_valid
            if peak is None or mean_abs > (peak.mean_abs_delta_db or 0.0):
                peak = block

    scene_mean_abs = round(weighted_abs / total_valid, 2) if total_valid else None
    scene_changed = round(total_changed / total_valid, 3) if total_valid else 0.0
    peak_direction = None
    if peak is not None and peak.mean_delta_db is not None:
        peak_direction = "brighter" if peak.mean_delta_db >= 0 else "dimmer"

    return ChangeStats(
        grid_rows=grid,
        grid_cols=grid,
        change_threshold_db=change_threshold_db,
        bounds=(float(left), float(bottom), float(right), float(top)),
        blocks=blocks,
        scene_mean_abs_delta_db=scene_mean_abs,
        scene_changed_fraction=scene_changed,
        peak_compass=peak.compass if peak else None,
        peak_direction=peak_direction,
        peak_mean_delta_db=peak.mean_delta_db if peak else None,
    )


def _split_slices(length: int, parts: int) -> list[slice]:
    """Contiguous, near-equal slices partitioning ``range(length)`` into ``parts``.

    Always returns exactly ``parts`` slices so the grid dimensions stay fixed; if
    ``length < parts`` the surplus slices are empty (rendered as unimaged cells).
    """
    base, extra = divmod(length, parts)
    slices: list[slice] = []
    start = 0
    for i in range(parts):
        step = base + (1 if i < extra else 0)
        slices.append(slice(start, start + step))
        start += step
    return slices


# --- The narration (a validated, provenance-stamped model reading) ----------


@dataclass
class ChangeNarration:
    """A validated, provenance-stamped model narration of two-pass SAR change.

    Every field has passed through :func:`parse_narration`. ``summary`` is a
    short plain-language paragraph; ``changes`` and ``caveats`` are lists of short
    strings; ``confidence`` is the model's own hedge. ``change_stats`` is the
    deterministic grid the narration is grounded in (embedded so the JSON output
    is self-contained and auditable). ``attribution`` and ``provenance`` are
    filled in deterministically -- the model never sets them.
    """

    item_ids: list[str]
    period_start: str | None
    period_end: str | None
    summary: str
    changes: list[str] = field(default_factory=list)
    confidence: str | None = None
    caveats: list[str] = field(default_factory=list)
    change_stats: dict[str, Any] | None = None
    model: str | None = None
    asset: str = "GEC"
    attribution: str = ATTRIBUTION
    provenance: str = AI_PROVENANCE

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serialisable view of the narration (for the sidecar)."""
        return {
            "item_ids": self.item_ids,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "summary": self.summary,
            "changes": self.changes,
            "confidence": self.confidence,
            "caveats": self.caveats,
            "change_stats": self.change_stats,
            "model": self.model,
            "asset": self.asset,
            "attribution": self.attribution,
            "provenance": self.provenance,
        }

    def to_text(self) -> str:
        """A human-readable rendering for the CLI's default (non-JSON) output."""
        span = ""
        if self.period_start or self.period_end:
            span = f" ({self.period_start or '?'} → {self.period_end or '?'})"
        lines = [f"Change narration{span}:", "", self.summary.strip()]
        if self.changes:
            lines.append("")
            lines.append("Observed changes:")
            lines.extend(f"  - {chg}" for chg in self.changes)
        if self.caveats:
            lines.append("")
            lines.append("Caveats:")
            lines.extend(f"  - {cav}" for cav in self.caveats)
        if self.confidence:
            lines.append("")
            lines.append(f"Confidence: {self.confidence}")
        if self.change_stats:
            peak = self.change_stats.get("peak_compass")
            direction = self.change_stats.get("peak_direction")
            mean_abs = self.change_stats.get("scene_mean_abs_delta_db")
            if peak and direction:
                lines.append("")
                lines.append(
                    f"Grounding: strongest change in the {peak} "
                    f"(got {direction}); scene mean |Δ| ≈ {mean_abs} dB."
                )
        lines.append("")
        lines.append(self.provenance)
        lines.append(self.attribution)
        return "\n".join(lines)


# --- Prompt construction (deterministic) ------------------------------------

_SYSTEM_PROMPT = """\
You are a SAR change analyst. You are shown ONE multi-temporal change composite of
an Umbra open-data site imaged on two (or three) dates, a JSON card describing the
acquisitions, and a coarse grid of the measured backscatter change in decibels.
Narrate ONLY the change the numbers and image support. Do not speculate about
purpose, ownership, or events you cannot see, and do not invent coordinates,
dates, or magnitudes -- the card and the dB grid carry the ground truth.

{primer}

Reading the change composite (two dates, earliest -> latest):
- GREEN  = backscatter that APPEARED / brightened in the later pass (something new
  or newly reflective: a ship, vehicles, construction, a filled lot).
- MAGENTA = backscatter that VANISHED / dimmed (something left, was removed, or a
  surface smoothed -- e.g. water rose over a field).
- GRAY / WHITE = unchanged between passes.
(For three dates the composite is a red->green->blue temporal trail instead.)

The dB change grid is north-up. Each cell is the mean SIGNED change in that part
of the scene: POSITIVE = brighter in the later pass (green), NEGATIVE = dimmer
(magenta), near zero = stable. Cite the grid: tie each change you report to a
compass direction and an approximate dB magnitude, and do NOT report change in a
region the grid shows as near zero.

Return ONE JSON object and nothing else -- no prose, no code fence. Use exactly
these keys:

  summary     string        -- 2-4 sentences: did the scene change materially
                               between the passes, where, and in which direction
                               (brighter/dimmer), in plain language.
  changes     array[string] -- short concrete phrases for distinct changes you can
                               see AND the grid supports (e.g. "brightening in the
                               northeast, ~+6 dB, consistent with new structures").
                               [] if the scene is essentially unchanged.
  confidence  string        -- your confidence the reading is correct: "low",
                               "medium", or "high".
  caveats     array[string] -- SAR-specific cautions (speckle, a dark area that
                               could be shadow or water, polarization mismatch,
                               apparent change that could be look-geometry).

If the grid and image show little change, say so plainly rather than inventing it.
"""


def _change_card(items: Sequence[UmbraItem], asset: str) -> dict[str, Any]:
    """The ground-truth metadata card for the compared acquisitions.

    Compact and deterministic: the ordered frame dates, the polarizations (with
    the change-detection caveat), the shared bbox and place, and the mandatory
    attribution -- so the model reads the facts rather than guessing them.
    """
    frames = [
        {
            "id": it.id,
            "datetime": it.datetime.isoformat() if it.datetime else None,
            "polarizations": it.polarizations,
        }
        for it in items
    ]
    pols = {tuple(it.polarizations) for it in items}
    first = items[0]
    return {
        "asset": asset,
        "place": first.task,
        "bbox": list(first.bbox) if first.bbox else None,
        "frames": frames,
        "frame_count": len(frames),
        "mixed_polarizations": len(pols) > 1,
        "polarization_caveat": POLARIZATION_CAVEAT,
        "attribution": ATTRIBUTION,
    }


def build_narrate_messages(
    change_card: dict[str, Any],
    stats: ChangeStats,
    image_png: bytes,
) -> dict[str, Any]:
    """Build the ``{"system", "user", "image_png"}`` prompt for a vision model.

    Deterministic and offline: the system prompt embeds the SAR primer, the
    composite's color legend, and the required JSON schema; the user message
    carries the acquisition card and the dB change grid (both the compact
    scene-level numbers and the north-up heat-grid) as ground truth, and
    ``image_png`` is the rendered change composite. This is what an injectable
    :data:`Narrator` receives.
    """
    system = _SYSTEM_PROMPT.format(primer=_SAR_PRIMER)
    card = json.dumps(change_card, indent=2)
    scene = {
        "grid_rows": stats.grid_rows,
        "grid_cols": stats.grid_cols,
        "change_threshold_db": stats.change_threshold_db,
        "scene_mean_abs_delta_db": stats.scene_mean_abs_delta_db,
        "scene_changed_fraction": stats.scene_changed_fraction,
        "peak_compass": stats.peak_compass,
        "peak_direction": stats.peak_direction,
        "peak_mean_delta_db": stats.peak_mean_delta_db,
    }
    user = (
        "Acquisitions compared (ground truth -- do not contradict):\n"
        f"{card}\n\n"
        "Measured change, scene-level (ground truth):\n"
        f"{json.dumps(scene, indent=2)}\n\n"
        "Measured signed dB change per grid cell, north-up "
        "(+ = brighter later, - = dimmer, . = not imaged on both passes):\n"
        f"{stats.to_grid_text()}\n\n"
        "Narrate what changed between the passes, grounded in these numbers."
    )
    return {"system": system, "user": user, "image_png": image_png}


# --- Reply parsing & validation (the interpretation boundary) ---------------

_CONFIDENCE_LEVELS = ("low", "medium", "high")


def parse_narration(
    raw: dict[str, Any],
    *,
    item_ids: list[str] | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    change_stats: dict[str, Any] | None = None,
    model: str | None = None,
    asset: str = "GEC",
) -> ChangeNarration:
    """Validate a model's raw reply into a :class:`ChangeNarration`.

    **This is the interpretation boundary.** The model's text is checked into a
    fixed shape -- ``summary`` must be a non-empty string, the two lists are
    coerced to clean string lists, ``confidence`` is normalised to
    ``low``/``medium``/``high`` (or dropped) -- and the mandatory
    :data:`~umbra_py.constants.ATTRIBUTION` and
    :data:`~umbra_py.constants.AI_PROVENANCE` are stamped on deterministically,
    never taken from the model. The deterministic ``change_stats`` grid is carried
    through unchanged. Unknown keys are ignored. Raises :class:`NarrateError` on a
    missing or ill-typed ``summary``.
    """
    if not isinstance(raw, dict):
        raise NarrateError(f"Expected a JSON object reply, got {type(raw).__name__}.")

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise NarrateError("The model reply is missing a non-empty 'summary' string.")

    confidence = raw.get("confidence")
    if isinstance(confidence, str):
        confidence = confidence.strip().lower() or None
        if confidence is not None and confidence not in _CONFIDENCE_LEVELS:
            confidence = None
    else:
        confidence = None

    try:
        changes = _coerce_str_list(raw.get("changes"), "changes")
        caveats = _coerce_str_list(raw.get("caveats"), "caveats")
    except UmbraError as exc:  # DescribeError -> surface as a NarrateError
        raise NarrateError(str(exc)) from exc

    return ChangeNarration(
        item_ids=list(item_ids or []),
        period_start=period_start,
        period_end=period_end,
        summary=summary.strip(),
        changes=changes,
        confidence=confidence,
        caveats=caveats,
        change_stats=change_stats,
        model=model,
        asset=asset,
    )


# --- Rendering the scene (deterministic; the picture + numbers) --------------


def render_change_png(
    items: Sequence[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    grid: int = 6,
    change_threshold_db: float = 3.0,
) -> tuple[bytes, ChangeStats]:
    """Render the change composite and compute its dB change grid in one pass.

    Co-registers the 2-3 acquisitions onto a single grid *once* (the expensive
    step -- range reads of each cloud-optimized GeoTIFF's overview), then both
    renders the same composite :func:`umbra_py.change_composite` produces and
    measures :func:`compute_change_stats` between the earliest and latest band.
    Returns ``(composite_png, stats)``: the PNG the model looks at and the
    deterministic grid it is grounded in.

    The change magnitudes are always measured in decibels regardless of the
    composite's ``db`` display stretch -- ``db`` only affects the picture's
    contrast, not the physics. Requires the ``viz`` extra.
    """
    # Validate the input before requiring the viz extra, so a bad item count is
    # the same ValueError with or without the extra installed.
    items = list(items)
    if len(items) not in (2, 3):
        raise ValueError(f"change narration needs 2 or 3 acquisitions, got {len(items)}.")

    from io import BytesIO  # noqa: PLC0415

    from .viz import _compose_change_rgba, _coregister_bands, _require  # noqa: PLC0415

    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    try:
        bands, bounds = _coregister_bands(items, asset, max_size)
    except MissingDependencyError:
        raise
    except UmbraError:
        raise
    except Exception as exc:  # rasterio / GDAL surface a variety of read errors
        raise NarrateError(f"Could not read the acquisitions to compare: {exc}") from exc

    stats = compute_change_stats(
        bands[0], bands[-1], bounds, grid=grid, change_threshold_db=change_threshold_db
    )
    rgba = _compose_change_rgba(bands, percentile=percentile, db=db)
    buf = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue(), stats


def save_change_scene(png_bytes: bytes, dest: str | os.PathLike) -> Any:
    """Write composite PNG bytes to ``dest``, flattening alpha for a JPEG target.

    A tiny helper so the CLI can persist the *same* composite bytes it hands the
    model (rather than re-rendering) while still honouring a ``.jpg`` ``--out``.
    Returns the written :class:`~pathlib.Path`.
    """
    from pathlib import Path  # noqa: PLC0415

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        from io import BytesIO  # noqa: PLC0415

        from .viz import _require  # noqa: PLC0415

        _require("PIL")
        from PIL import Image  # noqa: PLC0415

        Image.open(BytesIO(png_bytes)).convert("RGB").save(str(dest))
    else:
        dest.write_bytes(png_bytes)
    return dest


# --- The model boundary (the only part that calls a model) ------------------


def default_narrator(*, model: str | None = None) -> Narrator:
    """Build a :data:`Narrator` from environment variables.

    Reuses the exact provider plumbing of :func:`umbra_py.describe.default_describer`
    (the multimodal message contract is identical): Anthropic when
    ``ANTHROPIC_API_KEY`` is set, else an OpenAI-compatible endpoint when
    ``OPENAI_API_KEY`` is set. ``UMBRA_NARRATE_MODEL`` (or ``model=`` / ``--model``)
    overrides the model. Raises :class:`umbra_py.MissingDependencyError` with setup
    guidance when no key is configured -- the feature never runs without an
    explicit, user-supplied key.
    """
    model = model or os.environ.get("UMBRA_NARRATE_MODEL")
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
        "umbra change --narrate needs a vision model API key. Set ANTHROPIC_API_KEY "
        "(or OPENAI_API_KEY, optionally with OPENAI_BASE_URL for a compatible "
        "endpoint) and, optionally, UMBRA_NARRATE_MODEL to pick the model. The "
        "model only interprets the change composite; every narration is stamped as "
        "an AI interpretation and carries the CC-BY attribution."
    )


def narrate(
    items: Iterable[UmbraItem],
    *,
    narrator: Narrator | None = None,
    render: ChangeRenderer | None = None,
    model: str | None = None,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    grid: int = 6,
    change_threshold_db: float = 3.0,
) -> ChangeNarration:
    """Render two-pass change and return a validated :class:`ChangeNarration`.

    Renders the composite and its dB grid (:func:`render_change_png` by default,
    or an injected ``render``), builds the multimodal prompt from the picture, the
    acquisition card and the change grid, calls the ``narrator`` (default:
    :func:`default_narrator`, chosen from environment keys), and validates the
    reply through :func:`parse_narration`. The returned narration embeds the
    deterministic grid and carries the mandatory attribution and AI-provenance
    note.

    Pass the acquisitions in chronological order (2 or 3 of them). The model is
    *only* consulted to narrate the rendered change; inject a ``narrator`` and/or
    a ``render`` in tests to avoid any network call or the ``viz`` extra.
    """
    items = list(items)
    if len(items) not in (2, 3):
        raise NarrateError(f"change narration needs 2 or 3 acquisitions, got {len(items)}.")

    render_fn = render or (
        lambda its: render_change_png(
            its,
            asset=asset,
            max_size=max_size,
            percentile=percentile,
            db=db,
            grid=grid,
            change_threshold_db=change_threshold_db,
        )
    )
    image_png, stats = render_fn(items)

    card = _change_card(items, asset)
    narrate_fn = narrator or default_narrator(model=model)
    reply = narrate_fn(build_narrate_messages(card, stats, image_png))
    try:
        raw = _extract_json_object(reply)
    except UmbraError as exc:  # DescribeError -> surface as this module's error
        raise NarrateError(str(exc)) from exc
    return parse_narration(
        raw,
        item_ids=[it.id for it in items],
        period_start=items[0].datetime.isoformat() if items[0].datetime else None,
        period_end=items[-1].datetime.isoformat() if items[-1].datetime else None,
        change_stats=stats.to_dict(),
        model=model,
        asset=asset,
    )
