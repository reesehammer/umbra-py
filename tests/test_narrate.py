"""Offline tests for ``umbra change --narrate`` (the VLM change narration in
:mod:`umbra_py.narrate`).

No test calls a model or touches the network: the model step is an injectable
:data:`~umbra_py.narrate.Narrator` and the render step an injectable
:data:`~umbra_py.narrate.ChangeRenderer`, so these exercise the deterministic dB
change grid (:func:`compute_change_stats`), the prompt construction, the
interpretation boundary (:func:`parse_narration`), and the CLI wiring with fakes.
The one render test stubs the co-registration so no HTTP or GDAL read happens.
"""

from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

import umbra_py.narrate  # noqa: F401  (ensure the submodule is imported)
from umbra_py import ChangeNarration, compute_change_stats, narrate
from umbra_py.cli import cli
from umbra_py.constants import AI_PROVENANCE, ATTRIBUTION
from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem
from umbra_py.narrate import (
    NarrateError,
    build_narrate_messages,
    default_narrator,
    parse_narration,
    render_change_png,
)

narrate_mod = sys.modules["umbra_py.narrate"]

PNG = b"\x89PNG\r\n\x1a\n-fake-change-composite"


def _fake_narrator(reply):
    seen = {}

    def narrator(messages):
        seen["messages"] = messages
        return reply

    narrator.seen = seen
    return narrator


def _two_items():
    a = UmbraItem(
        id="scene-a",
        bbox=(0.0, 0.0, 1.0, 1.0),
        properties={"datetime": "2024-01-01T00:00:00Z", "sar:polarizations": ["VV"]},
    )
    b = UmbraItem(
        id="scene-b",
        bbox=(0.0, 0.0, 1.0, 1.0),
        properties={"datetime": "2024-03-01T00:00:00Z", "sar:polarizations": ["VV"]},
    )
    return [a, b]


# --- compute_change_stats: the deterministic grounding ----------------------


def test_change_stats_measures_signed_db_and_locates_the_peak():
    np = pytest.importorskip("numpy")
    earlier = np.ones((12, 12), dtype="float32")
    later = np.ones((12, 12), dtype="float32")
    # Brighten the north-east corner strongly (row 0 = north, high col = east).
    later[0:4, 8:12] = 10.0
    stats = compute_change_stats(earlier, later, (0.0, 0.0, 1.0, 1.0), grid=3)

    assert stats.grid_rows == 3 and stats.grid_cols == 3
    assert len(stats.blocks) == 9
    # 20*log10(10) = 20 dB brighter in that block.
    ne = next(b for b in stats.blocks if b.compass == "northeast")
    assert ne.mean_delta_db == pytest.approx(20.0)
    assert ne.brightened_fraction == 1.0 and ne.dimmed_fraction == 0.0
    # An unchanged block reads ~0 dB.
    center = next(b for b in stats.blocks if b.compass == "center")
    assert center.mean_delta_db == pytest.approx(0.0)
    # Scene-level peak points at the corner that moved.
    assert stats.peak_compass == "northeast"
    assert stats.peak_direction == "brighter"
    assert stats.peak_mean_delta_db == pytest.approx(20.0)


def test_change_stats_flags_dimming_as_magenta_direction():
    np = pytest.importorskip("numpy")
    earlier = np.full((8, 8), 10.0, dtype="float32")
    later = np.full((8, 8), 10.0, dtype="float32")
    later[:, :] = 10.0
    later[4:8, 0:4] = 1.0  # south-west dims by -20 dB
    stats = compute_change_stats(earlier, later, (0.0, 0.0, 1.0, 1.0), grid=2)
    sw = next(b for b in stats.blocks if b.compass == "southwest")
    assert sw.mean_delta_db == pytest.approx(-20.0)
    assert sw.dimmed_fraction == 1.0
    assert stats.peak_direction == "dimmer"


def test_change_stats_excludes_pixels_not_imaged_on_both_passes():
    np = pytest.importorskip("numpy")
    earlier = np.ones((6, 6), dtype="float32")
    later = np.ones((6, 6), dtype="float32")
    earlier[0:3, :] = 0.0  # north half unimaged in the earlier pass (nodata)
    stats = compute_change_stats(earlier, later, (0.0, 0.0, 1.0, 1.0), grid=2)
    north = next(b for b in stats.blocks if b.compass.startswith("north"))
    assert north.valid_fraction == 0.0
    assert north.mean_delta_db is None
    # The grid text renders an unimaged block as a dot, not a bogus number.
    assert "." in stats.to_grid_text()


def test_change_stats_rejects_mismatched_shapes():
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="share a shape"):
        compute_change_stats(np.ones((4, 4)), np.ones((4, 5)), (0, 0, 1, 1))


def test_change_stats_to_dict_is_json_serialisable():
    np = pytest.importorskip("numpy")
    stats = compute_change_stats(np.ones((6, 6)), np.full((6, 6), 4.0), (0, 0, 1, 1), grid=2)
    data = stats.to_dict()
    json.dumps(data)  # must not raise
    assert data["grid_rows"] == 2
    assert len(data["blocks"]) == 4


# --- build_narrate_messages -------------------------------------------------


def test_build_messages_embeds_primer_legend_grid_and_image():
    np = pytest.importorskip("numpy")
    stats = compute_change_stats(np.ones((6, 6)), np.full((6, 6), 4.0), (0, 0, 1, 1), grid=2)
    messages = build_narrate_messages({"place": "Centerfield, Utah"}, stats, PNG)
    assert set(messages) == {"system", "user", "image_png"}
    assert messages["image_png"] is PNG
    # SAR literacy + the composite color legend live in the system prompt.
    assert "backscatter" in messages["system"]
    assert "GREEN" in messages["system"] and "MAGENTA" in messages["system"]
    assert "JSON object" in messages["system"]
    # The metadata card and the numeric grid travel as ground truth.
    assert "Centerfield, Utah" in messages["user"]
    assert "per grid cell" in messages["user"]


# --- parse_narration: the interpretation boundary ---------------------------


def test_parse_narration_validates_and_stamps_provenance():
    narration = parse_narration(
        {
            "summary": "  The north-east brightened between the passes.  ",
            "changes": ["new bright returns in the northeast, ~+6 dB", "  "],
            "confidence": "High",
            "caveats": ["speckle in the calm water"],
            "extra": "ignored",
        },
        item_ids=["a", "b"],
        period_start="2024-01-01T00:00:00+00:00",
        period_end="2024-03-01T00:00:00+00:00",
        change_stats={"peak_compass": "northeast"},
        model="m",
    )
    assert narration.summary == "The north-east brightened between the passes."
    assert narration.changes == ["new bright returns in the northeast, ~+6 dB"]
    assert narration.confidence == "high"
    assert narration.caveats == ["speckle in the calm water"]
    assert narration.item_ids == ["a", "b"]
    assert narration.period_end.startswith("2024-03-01")
    assert narration.change_stats == {"peak_compass": "northeast"}
    # Provenance and attribution are stamped deterministically, not from the model.
    assert narration.attribution == ATTRIBUTION
    assert narration.provenance == AI_PROVENANCE


def test_parse_narration_requires_a_summary():
    with pytest.raises(NarrateError, match="summary"):
        parse_narration({"changes": ["x"]})
    with pytest.raises(NarrateError, match="summary"):
        parse_narration({"summary": "   "})


def test_parse_narration_drops_off_menu_confidence():
    narration = parse_narration({"summary": "s", "confidence": "very-sure"})
    assert narration.confidence is None


def test_parse_narration_rejects_non_string_change_entries():
    with pytest.raises(NarrateError, match="changes"):
        parse_narration({"summary": "s", "changes": [1, 2]})


def test_narration_to_text_carries_grounding_and_provenance():
    narration = ChangeNarration(
        item_ids=["a", "b"],
        period_start="2024-01-01",
        period_end="2024-03-01",
        summary="Material brightening in the northeast.",
        changes=["bright new returns northeast"],
        confidence="medium",
        caveats=["could be look-geometry"],
        change_stats={
            "peak_compass": "northeast",
            "peak_direction": "brighter",
            "scene_mean_abs_delta_db": 3.4,
        },
    )
    text = narration.to_text()
    assert "Material brightening" in text
    assert "bright new returns northeast" in text
    assert "could be look-geometry" in text
    assert "Confidence: medium" in text
    assert "strongest change in the northeast" in text
    assert AI_PROVENANCE in text
    assert ATTRIBUTION in text


# --- narrate(): end-to-end with injected narrator + render ------------------


def test_narrate_uses_injected_render_and_narrator():
    np = pytest.importorskip("numpy")
    items = _two_items()
    stats = compute_change_stats(np.ones((6, 6)), np.full((6, 6), 4.0), (0, 0, 1, 1), grid=2)
    reply = json.dumps({"summary": "Brighter overall.", "confidence": "low"})
    narrator = _fake_narrator(reply)

    narration = narrate(
        items,
        narrator=narrator,
        render=lambda _its: (PNG, stats),
    )
    assert narration.summary == "Brighter overall."
    assert narration.confidence == "low"
    assert narration.item_ids == ["scene-a", "scene-b"]
    assert narration.period_start.startswith("2024-01-01")
    assert narration.period_end.startswith("2024-03-01")
    # The deterministic grid is embedded for auditing.
    assert narration.change_stats["grid_rows"] == 2
    # The rendered PNG reached the narrator.
    assert narrator.seen["messages"]["image_png"] is PNG


def test_narrate_extracts_json_from_a_fenced_reply():
    np = pytest.importorskip("numpy")
    items = _two_items()
    stats = compute_change_stats(np.ones((4, 4)), np.ones((4, 4)), (0, 0, 1, 1), grid=2)
    reply = 'Here:\n```json\n{"summary": "No material change."}\n```'
    narration = narrate(items, narrator=_fake_narrator(reply), render=lambda _i: (PNG, stats))
    assert narration.summary == "No material change."


def test_narrate_raises_on_wrong_item_count():
    with pytest.raises(NarrateError, match="2 or 3"):
        narrate([_two_items()[0]], narrator=_fake_narrator("{}"), render=lambda _i: (PNG, None))


def test_narrate_raises_when_reply_has_no_json():
    np = pytest.importorskip("numpy")
    items = _two_items()
    stats = compute_change_stats(np.ones((4, 4)), np.ones((4, 4)), (0, 0, 1, 1), grid=2)
    with pytest.raises(NarrateError, match="did not contain a JSON object"):
        narrate(items, narrator=lambda m: "I can't tell.", render=lambda _i: (PNG, stats))


# --- render_change_png: one co-registration, PNG + stats --------------------


def test_render_change_png_returns_png_and_stats(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import umbra_py.viz as viz_mod

    earlier = np.ones((16, 16), dtype="float32")
    later = np.ones((16, 16), dtype="float32")
    later[0:8, 8:16] = 8.0
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([earlier, later], (0.0, 0.0, 1.0, 1.0))
    )
    png, stats = render_change_png(_two_items(), grid=2)
    assert png.startswith(b"\x89PNG\r\n")
    assert stats.grid_rows == 2
    # first-vs-last brightening shows up in the grid.
    assert stats.peak_direction == "brighter"


def test_render_change_png_rejects_wrong_count():
    with pytest.raises(ValueError, match="2 or 3"):
        render_change_png([_two_items()[0]])


# --- default_narrator: provider selection from env (no network) -------------


def test_default_narrator_errors_without_a_key(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingDependencyError, match="vision model API key"):
        default_narrator()


def test_default_narrator_prefers_anthropic(monkeypatch):
    # ``from umbra_py import describe`` (the function) shadows the submodule
    # attribute, so reach the real module through sys.modules to monkeypatch it.
    describe_mod = sys.modules["umbra_py.describe"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        return {"content": [{"type": "text", "text": '{"summary": "ok"}'}]}

    monkeypatch.setattr(describe_mod, "_post_json", fake_post)
    narrator = default_narrator(model="claude-vision-test")
    text = narrator({"system": "s", "user": "u", "image_png": PNG})
    assert "api.anthropic.com" in captured["url"]
    assert '"summary": "ok"' in text


# --- CLI: umbra change --narrate --------------------------------------------


@pytest.fixture
def fixed_narration(monkeypatch, sample_item_dict):
    """Point the CLI's change render + narrator at fixed values so `umbra change
    --narrate` runs end-to-end without a model, network, or a real GDAL read."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import umbra_py.viz as viz_mod

    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    earlier = np.ones((16, 16), dtype="float32")
    later = np.ones((16, 16), dtype="float32")
    later[0:8, 8:16] = 8.0
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([earlier, later], (0.0, 0.0, 1.0, 1.0))
    )
    reply = json.dumps(
        {
            "summary": "The northeast brightened between the two passes.",
            "changes": ["new bright returns in the northeast, ~+9 dB"],
            "confidence": "medium",
            "caveats": ["apparent change could reflect look-geometry"],
        }
    )
    monkeypatch.setattr(narrate_mod, "default_narrator", lambda **k: lambda m: reply)
    return reply


def test_cli_change_narrate_writes_image_sidecar_and_prints(fixed_narration, tmp_path):
    out = tmp_path / "change.png"
    result = CliRunner().invoke(
        cli,
        ["change", "http://x/a.json", "http://x/b.json", "--out", str(out), "--narrate"],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "Wrote change composite" in result.output
    # The narration text is printed with grounding + provenance.
    assert "northeast brightened" in result.output
    assert "Observed changes:" in result.output
    assert AI_PROVENANCE in result.output
    # The machine-readable sidecar is written next to the image and is auditable.
    sidecar = tmp_path / "change.narration.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["summary"].startswith("The northeast brightened")
    assert data["change_stats"]["grid_rows"] >= 1
    assert data["change_stats"]["peak_direction"] == "brighter"
    assert data["attribution"] == ATTRIBUTION
    assert data["provenance"] == AI_PROVENANCE


def test_cli_change_narrate_rejects_gif(monkeypatch, sample_item_dict, tmp_path):
    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli,
        [
            "change",
            "http://x/a.json",
            "http://x/b.json",
            "--out",
            str(tmp_path / "c.gif"),
            "--narrate",
        ],
    )
    assert result.exit_code != 0
    assert "not a .gif" in result.output


def test_cli_change_model_requires_narrate(monkeypatch, sample_item_dict, tmp_path):
    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli,
        [
            "change",
            "http://x/a.json",
            "http://x/b.json",
            "--out",
            str(tmp_path / "c.png"),
            "--model",
            "some-model",
        ],
    )
    assert result.exit_code != 0
    assert "--model only applies" in result.output


def test_cli_change_narrate_reports_missing_key_cleanly(monkeypatch, sample_item_dict, tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import umbra_py.viz as viz_mod

    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    earlier = np.ones((8, 8), dtype="float32")
    monkeypatch.setattr(
        viz_mod, "_coregister_bands", lambda *a, **k: ([earlier, earlier.copy()], (0, 0, 1, 1))
    )
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    out = tmp_path / "change.png"
    result = CliRunner().invoke(
        cli,
        ["change", "http://x/a.json", "http://x/b.json", "--out", str(out), "--narrate"],
    )
    assert result.exit_code != 0
    assert "vision model API key" in result.output
