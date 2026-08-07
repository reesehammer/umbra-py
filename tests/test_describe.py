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
from umbra_py.index import BakedPreview
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
    from umbra_py.viz import raster as viz_mod

    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    data = np.linspace(0, 1, 64 * 64, dtype="float32").reshape(64, 64)
    monkeypatch.setattr(viz_mod, "_read_sar_band", lambda *a, **k: (data, None))
    png = render_quicklook_png(item, max_size=64)
    assert png.startswith(b"\x89PNG\r\n")
    assert len(png) > 8


def test_render_quicklook_png_wraps_read_errors(monkeypatch, sample_item_dict):
    pytest.importorskip("PIL")
    from umbra_py.viz import raster as viz_mod

    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")

    def boom(*a, **k):
        raise ValueError("range read failed")

    monkeypatch.setattr(viz_mod, "_read_sar_band", boom)
    with pytest.raises(DescribeError, match="Could not render a quicklook"):
        render_quicklook_png(item)


# --- default_describer: provider selection from env (no network) ------------


def test_default_describer_errors_without_a_key(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
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
    # The completion is bounded (matching the Anthropic path): an omitted
    # max_tokens makes a gateway like OpenRouter reserve the model's whole
    # output budget against the key's limit and 402 the call.
    assert captured["payload"]["max_tokens"] == 1024
    assert '"summary": "reservoir"' in text


def test_default_describer_routes_an_openrouter_key_to_openrouter(monkeypatch):
    """A single OPENROUTER_API_KEY reaches OpenRouter's OpenAI-compatible host,
    with its default model and the ranking headers -- and wins over a stray
    OPENAI_API_KEY, since setting the OpenRouter key is an unambiguous opt-in."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-stray")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["model"] = payload["model"]
        return {"choices": [{"message": {"content": '{"summary": "via openrouter"}'}}]}

    monkeypatch.setattr(describe_mod, "_post_json", fake_post)
    describer = default_describer()
    text = describer({"system": "s", "user": "u", "image_png": PNG})
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"
    assert captured["model"] == "openai/gpt-4o-mini"
    # OpenRouter's ranking headers ride along (optional there, ignored elsewhere).
    assert captured["headers"]["X-Title"] == "umbra-py"
    assert "HTTP-Referer" in captured["headers"]
    assert '"summary": "via openrouter"' in text


def test_default_describer_openrouter_honors_base_url_and_model(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://gateway.example/v1")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["model"] = payload["model"]
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(describe_mod, "_post_json", fake_post)
    default_describer(model="anthropic/claude-3.5-sonnet")(
        {"system": "s", "user": "u", "image_png": PNG}
    )
    assert captured["url"] == "https://gateway.example/v1/chat/completions"
    assert captured["model"] == "anthropic/claude-3.5-sonnet"


# --- CLI: umbra describe ----------------------------------------------------


@pytest.fixture
def fixed_description(monkeypatch, sample_item_dict):
    """Point the CLI's default describer + render at fixed values so ``umbra
    describe`` runs end-to-end without a model or the viz extra."""
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
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
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json"])
    assert result.exit_code != 0
    assert "vision model API key" in result.output


def test_cli_describe_reports_a_bad_reply_cleanly(monkeypatch, sample_item_dict):
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)
    monkeypatch.setattr(describe_mod, "default_describer", lambda **k: lambda m: "no json here")
    result = CliRunner().invoke(cli, ["describe", "https://example/item.json"])
    assert result.exit_code != 0
    assert "did not contain a JSON object" in result.output


# --- The baked preview: reading the picture this machine already has --------


def _png(width, height, tail=b""):
    """Minimal but real PNG header bytes, so ``_png_size`` measures a picture."""
    import struct

    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    ) + tail


def _previews(png=None, seen=None, asset=None, max_size=None):
    """A `BakedPreviews` stand-in returning the record for ``png``, or nothing baked.

    ``asset`` left as ``None`` is the unrecorded preview an index baked before it
    kept track of what it rendered -- the case that still falls back to assuming
    the default bake.
    """

    def lookup(item_id):
        if seen is not None:
            seen.append(item_id)
        return None if png is None else BakedPreview(png=png, asset=asset, max_size=max_size)

    return lookup


def test_png_size_reads_the_header_and_shrugs_at_anything_else():
    assert describe_mod._png_size(_png(256, 200)) == (256, 200)
    # A stub PNG from an injected renderer is not measurable -- that is provenance
    # missing, not an error.
    assert describe_mod._png_size(PNG) is None
    assert describe_mod._png_size(b"") is None


def test_baked_preview_refusal_names_what_the_bake_cannot_answer():
    from umbra_py.describe import baked_preview_refusal

    assert baked_preview_refusal() is None
    assert "GEC" in (baked_preview_refusal(asset="CSI") or "")
    assert "decibel" in (baked_preview_refusal(db=False) or "")


def test_baked_preview_refusal_reads_the_bake_rather_than_assuming_it():
    """With a record of what was baked, the check is against that -- so a CSI bake
    answers a CSI request, and a GEC one is refused naming what it actually is."""
    from umbra_py.describe import baked_preview_refusal

    csi = BakedPreview(png=PNG, asset="CSI", max_size=256)
    assert baked_preview_refusal(asset="CSI", baked=csi) is None
    assert "CSI quicklook" in (baked_preview_refusal(asset="GEC", baked=csi) or "")
    # The stretch is a property of every bake, so it is refused with a record too.
    assert "decibel" in (baked_preview_refusal(asset="CSI", baked=csi, db=False) or "")
    # An unrecorded preview keeps the assumed rule.
    assert "GEC" in (baked_preview_refusal(asset="CSI", baked=BakedPreview(png=PNG)) or "")


def test_default_preview_never_looks_at_the_index(sample_item_dict):
    """The default is unchanged by this option's existence: a fresh render, and
    not even a lookup against the cache."""
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    seen = []
    desc = describe(
        item,
        describer=_fake_describer('{"summary": "Farmland."}'),
        render=_stub_render(),
        previews=_previews(_png(256, 256), seen),
    )
    assert seen == []
    assert desc.image.source == "rendered"
    assert desc.caveats == []


def test_auto_preview_reads_the_baked_picture_and_says_so(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    describer = _fake_describer('{"summary": "A harbor."}')
    baked = _png(256, 256, tail=b"-baked")

    def boom(_item):
        raise AssertionError("rendered a scene that was already baked")

    desc = describe(
        item,
        describer=describer,
        render=boom,
        previews=_previews(baked),
        preview="auto",
    )
    # The model was handed the cached bytes, not a render.
    assert describer.seen["messages"]["image_png"] == baked
    assert desc.image.source == "baked"
    assert (desc.image.width, desc.image.height) == (256, 256)
    assert desc.image.max_size == 1024
    # ... and the description says the picture was smaller than the one asked for.
    assert any("256x256 px preview" in c for c in desc.caveats)
    assert desc.to_dict()["image"]["source"] == "baked"


def test_a_preview_at_least_as_big_as_the_render_needs_no_caveat(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    desc = describe(
        item,
        describer=_fake_describer('{"summary": "A harbor."}'),
        previews=_previews(_png(256, 256)),
        preview="auto",
        max_size=256,
    )
    assert desc.image.source == "baked"
    assert desc.caveats == []


def test_auto_preview_renders_what_is_not_baked(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    desc = describe(
        item,
        describer=_fake_describer('{"summary": "Farmland."}'),
        render=_stub_render(),
        previews=_previews(None),
        preview="auto",
    )
    assert desc.image.source == "rendered"
    assert desc.caveats == []


def test_auto_preview_renders_rather_than_substituting_a_different_picture(sample_item_dict):
    """A CSI request or a linear stretch is not a smaller version of the baked
    picture -- it is a different one, so ``auto`` renders instead of substituting."""
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    desc = describe(
        item,
        describer=_fake_describer('{"summary": "Farmland."}'),
        render=_stub_render(),
        previews=_previews(_png(256, 256), asset="GEC"),
        preview="auto",
        asset="CSI",
    )
    assert desc.image.source == "rendered"


def test_a_bake_of_the_asset_asked_for_is_used_rather_than_refused(sample_item_dict):
    """The record is what lifts the restriction: a deliberate CSI bake answers a
    CSI reading, where an index that stored only pixels had to refuse it."""
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")

    def boom(_item):
        raise AssertionError("rendered a scene that was already baked")

    desc = describe(
        item,
        describer=_fake_describer('{"summary": "A harbor."}'),
        render=boom,
        previews=_previews(_png(256, 256), asset="CSI"),
        preview="baked",
        asset="CSI",
    )
    assert desc.image.source == "baked"
    # The description reports the product it was actually read from.
    assert desc.image.asset == "CSI"
    assert desc.asset == "CSI"


def test_a_bake_of_another_asset_is_refused_naming_what_it_is(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with pytest.raises(DescribeError, match="CSI quicklook"):
        describe(
            item,
            describer=_fake_describer("{}"),
            previews=_previews(_png(256, 256), asset="CSI"),
            preview="baked",
            asset="GEC",
        )


def test_baked_preview_refuses_a_request_it_cannot_answer(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with pytest.raises(DescribeError, match="GEC quicklook"):
        describe(
            item,
            describer=_fake_describer("{}"),
            previews=_previews(_png(256, 256)),
            preview="baked",
            asset="CSI",
        )


def test_baked_preview_tells_no_index_apart_from_nothing_baked(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with pytest.raises(DescribeError, match="index fetch"):
        describe(item, describer=_fake_describer("{}"), preview="baked")
    with pytest.raises(DescribeError, match="bake-thumbnails"):
        describe(
            item,
            describer=_fake_describer("{}"),
            previews=_previews(None),
            preview="baked",
        )


def test_unknown_preview_source_is_rejected(sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with pytest.raises(DescribeError, match="Unknown preview source"):
        describe(item, describer=_fake_describer("{}"), preview="cached")


def test_cli_describe_reads_a_baked_preview_from_the_index(monkeypatch, tmp_path, sample_item_dict):
    """End to end: `umbra describe --preview baked --index-db` describes a scene
    from local bytes -- no S3 overview stream, no viz extra."""
    from umbra_py.index import CatalogIndex

    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.add(item)
        idx.commit()
        idx.bake_thumbnails(renderer=lambda _it: _png(128, 128, tail=b"-from-index"))
        idx.commit()

    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    monkeypatch.setattr(
        describe_mod,
        "default_describer",
        lambda **k: lambda m: json.dumps({"summary": "A quiet estuary.", "confidence": "low"}),
    )

    def boom(*_a, **_k):
        raise AssertionError("streamed a quicklook that was already baked")

    monkeypatch.setattr(describe_mod, "render_quicklook_png", boom)

    result = CliRunner().invoke(
        cli,
        [
            "describe",
            "https://example/item.json",
            "--preview",
            "baked",
            "--index-db",
            str(tmp_path / "catalog.db"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["image"] == {
        "source": "baked",
        "asset": "GEC",
        "width": 128,
        "height": 128,
        "max_size": 1024,
        "db": True,
    }
    assert any("128x128 px preview" in c for c in data["caveats"])


def test_cli_describe_baked_without_an_index_says_which_fetch_is_missing(
    monkeypatch, tmp_path, sample_item_dict
):
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(
        cli,
        [
            "describe",
            "https://example/item.json",
            "--preview",
            "baked",
            "--index-db",
            str(tmp_path / "absent.db"),
        ],
    )
    assert result.exit_code != 0
    assert "umbra index fetch" in result.output
