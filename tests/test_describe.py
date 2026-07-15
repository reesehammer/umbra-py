"""Offline tests for ``umbra describe`` (the VLM scene description in
:mod:`umbra_py.describe`).

No test calls a model or touches the network: the model step is an injectable
:data:`~umbra_py.describe.Describer` and the render step an injectable
:data:`~umbra_py.describe.Renderer`, so these exercise the deterministic prompt
construction, the interpretation boundary (:func:`parse_description`), the render
helper (with a stubbed SAR band), and the CLI wiring with fakes.
"""

from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner

import umbra_py.describe  # noqa: F401  (ensure the submodule is imported)
from umbra_py import SceneDescription, describe
from umbra_py.cli import cli
from umbra_py.constants import AI_PROVENANCE, ATTRIBUTION
from umbra_py.describe import (
    DescribeError,
    build_describe_messages,
    default_describer,
    parse_description,
    render_quicklook_png,
)
from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem

# ``from umbra_py import describe`` (the function) shadows the ``umbra_py.describe``
# submodule attribute, so fetch the real module object from sys.modules for
# monkeypatching its globals.
describe_mod = sys.modules["umbra_py.describe"]

PNG = b"\x89PNG\r\n\x1a\n-fake-quicklook-bytes"


def _fake_describer(reply):
    """A describer that ignores the prompt and returns a fixed reply string.

    Captures the payload it was handed so a test can assert on the prompt/image.
    """
    seen = {}

    def describer(messages):
        seen["messages"] = messages
        return reply

    describer.seen = seen
    return describer


def _stub_render(png=PNG):
    return lambda _item: png


# --- build_describe_messages ------------------------------------------------


def test_build_messages_embeds_primer_card_and_image():
    card = {"id": "abc", "place": "Centerfield, Utah", "attribution": ATTRIBUTION}
    messages = build_describe_messages(card, PNG)
    assert set(messages) == {"system", "user", "image_png"}
    assert messages["image_png"] is PNG
    # SAR literacy the model needs is in the system prompt, not left to memory.
    assert "backscatter" in messages["system"]
    assert "Layover" in messages["system"] or "layover" in messages["system"]
    assert "JSON object" in messages["system"]
    # The metadata card travels as ground truth in the user turn.
    assert "Centerfield, Utah" in messages["user"]


# --- parse_description: the interpretation boundary -------------------------


def test_parse_description_validates_and_stamps_provenance():
    desc = parse_description(
        {
            "summary": "  A bright urban grid beside a dark river.  ",
            "observed_features": ["bright grid northeast", "  ", "dark river south"],
            "confidence": "High",
            "caveats": ["dark area may be shadow or water"],
            "surprise": "ignored",
        },
        item_id="item-1",
        model="m",
        asset="GEC",
    )
    assert desc.summary == "A bright urban grid beside a dark river."
    # Blank list entries are dropped; strings are trimmed.
    assert desc.observed_features == ["bright grid northeast", "dark river south"]
    assert desc.confidence == "high"  # normalised to lowercase
    assert desc.caveats == ["dark area may be shadow or water"]
    assert desc.item_id == "item-1"
    # Provenance and attribution are stamped deterministically, not from the model.
    assert desc.attribution == ATTRIBUTION
    assert desc.provenance == AI_PROVENANCE


def test_parse_description_requires_a_summary():
    with pytest.raises(DescribeError, match="summary"):
        parse_description({"observed_features": ["x"]})
    with pytest.raises(DescribeError, match="summary"):
        parse_description({"summary": "   "})


def test_parse_description_drops_off_menu_confidence():
    desc = parse_description({"summary": "s", "confidence": "extremely-sure"})
    assert desc.confidence is None


def test_parse_description_accepts_string_lists_and_missing_fields():
    desc = parse_description({"summary": "s", "observed_features": "a single phrase"})
    assert desc.observed_features == ["a single phrase"]
    assert desc.caveats == []
    assert desc.confidence is None


def test_parse_description_rejects_non_string_list_entries():
    with pytest.raises(DescribeError, match="observed_features"):
        parse_description({"summary": "s", "observed_features": [1, 2]})


def test_scene_description_to_text_carries_provenance():
    desc = SceneDescription(
        item_id="i",
        summary="A calm harbor.",
        observed_features=["bright quay"],
        confidence="medium",
        caveats=["speckle in the water"],
    )
    text = desc.to_text()
    assert "A calm harbor." in text
    assert "bright quay" in text
    assert "speckle in the water" in text
    assert "Confidence: medium" in text
    assert AI_PROVENANCE in text
    assert ATTRIBUTION in text


def test_scene_description_to_dict_roundtrips():
    desc = SceneDescription(item_id="i", summary="s", observed_features=["a"])
    data = desc.to_dict()
    assert data["item_id"] == "i"
    assert data["summary"] == "s"
    assert data["attribution"] == ATTRIBUTION
    assert data["provenance"] == AI_PROVENANCE


# --- describe(): end-to-end with injected describer + render ----------------


def test_describe_extracts_json_from_a_fenced_reply(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    reply = 'Here you go:\n```json\n{"summary": "Flat farmland.", "confidence": "low"}\n```'
    describer = _fake_describer(reply)
    desc = describe(item, describer=describer, render=_stub_render())
    assert desc.summary == "Flat farmland."
    assert desc.confidence == "low"
    assert desc.item_id == item.id
    # The rendered PNG reached the describer, and the metadata card too.
    assert describer.seen["messages"]["image_png"] == PNG
    assert (
        item.id in describer.seen["messages"]["user"]
        or "Metadata card" in (describer.seen["messages"]["user"])
    )


def test_describe_extracts_json_with_surrounding_prose(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    reply = 'The scene shows {"summary": "A dark reservoir."} overall.'
    desc = describe(item, describer=_fake_describer(reply), render=_stub_render())
    assert desc.summary == "A dark reservoir."


def test_describe_raises_when_reply_has_no_json(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with pytest.raises(DescribeError, match="did not contain a JSON object"):
        describe(item, describer=lambda m: "I cannot see the image.", render=_stub_render())


# --- render_quicklook_png: PNG bytes from a stubbed SAR band ----------------


def test_render_quicklook_png_returns_png_bytes(monkeypatch, sample_item_dict):
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    import umbra_py.viz as viz_mod

    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    data = np.linspace(0, 1, 64 * 64, dtype="float32").reshape(64, 64)
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))
    png = render_quicklook_png(item, max_size=64)
    assert png.startswith(b"\x89PNG\r\n")
    assert len(png) > 8


def test_render_quicklook_png_wraps_read_errors(monkeypatch, sample_item_dict):
    pytest.importorskip("PIL")
    import umbra_py.viz as viz_mod

    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")

    def boom(*a, **k):
        raise ValueError("range read failed")

    monkeypatch.setattr(viz_mod, "_read_sar_band", boom)
    with pytest.raises(DescribeError, match="Could not render a quicklook"):
        render_quicklook_png(item)


# --- default_describer: provider selection from env (no network) ------------


def test_default_describer_errors_without_a_key(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingDependencyError, match="vision model API key"):
        default_describer()


def test_default_describer_prefers_anthropic_and_sends_image(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": '{"summary": "ok"}'}]}

    monkeypatch.setattr(describe_mod, "_post_json", fake_post)
    describer = default_describer(model="claude-vision-test")
    text = describer({"system": "s", "user": "u", "image_png": PNG})
    assert "api.anthropic.com" in captured["url"]
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    # The image is sent as a base64 image content block.
    blocks = captured["payload"]["messages"][0]["content"]
    image_blocks = [b for b in blocks if b.get("type") == "image"]
    assert image_blocks and image_blocks[0]["source"]["media_type"] == "image/png"
    assert '"summary": "ok"' in text


def test_default_describer_falls_back_to_openai_with_data_uri(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"choices": [{"message": {"content": '{"summary": "reservoir"}'}}]}

    monkeypatch.setattr(describe_mod, "_post_json", fake_post)
    describer = default_describer()
    text = describer({"system": "s", "user": "u", "image_png": PNG})
    assert captured["url"] == "https://proxy.example/v1/chat/completions"
    blocks = captured["payload"]["messages"][1]["content"]
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert image_blocks and image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert '"summary": "reservoir"' in text


# --- CLI: umbra describe ----------------------------------------------------


@pytest.fixture
def fixed_description(monkeypatch, sample_item_dict):
    """Point the CLI's default describer + render at fixed values so ``umbra
    describe`` runs end-to-end without a model or the viz extra."""
    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    reply = json.dumps(
        {
            "summary": "A bright industrial site surrounded by dark fields.",
            "observed_features": ["bright rectangular structures", "dark smooth fields"],
            "confidence": "medium",
            "caveats": ["dark fields could be low-backscatter crops or bare soil"],
        }
    )
    monkeypatch.setattr(describe_mod, "default_describer", lambda **k: lambda m: reply)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)
    return reply


def test_cli_describe_prints_a_reading(fixed_description):
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json"])
    assert result.exit_code == 0, result.output
    assert "bright industrial site" in result.output
    assert "Observed features:" in result.output
    assert "Confidence: medium" in result.output
    # Provenance and attribution are always shown.
    assert AI_PROVENANCE in result.output
    assert ATTRIBUTION in result.output


def test_cli_describe_json_emits_structured_output(fixed_description):
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["summary"].startswith("A bright industrial site")
    assert data["observed_features"]
    assert data["confidence"] == "medium"
    assert data["attribution"] == ATTRIBUTION
    assert data["provenance"] == AI_PROVENANCE


def test_cli_describe_reports_missing_key_cleanly(monkeypatch, sample_item_dict):
    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json"])
    assert result.exit_code != 0
    assert "vision model API key" in result.output


def test_cli_describe_reports_a_bad_reply_cleanly(monkeypatch, sample_item_dict):
    monkeypatch.setattr("umbra_py.cli.get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)
    monkeypatch.setattr(describe_mod, "default_describer", lambda **k: lambda m: "no json here")
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json"])
    assert result.exit_code != 0
    assert "did not contain a JSON object" in result.output
