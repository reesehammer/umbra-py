"""Offline tests for ``umbra ask`` (the model-planned, deterministically
executed natural-language search in :mod:`umbra_py.planner`).

No test calls a model: the planning step is an injectable callable, so these
exercise the deterministic determinism boundary (:func:`parse_plan`), the
command rendering, and the CLI wiring with a fake planner.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from click.testing import CliRunner

import umbra_py.planner as planner_mod
from umbra_py import ask
from umbra_py.cli import cli
from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem
from umbra_py.planner import (
    AskError,
    SearchPlan,
    build_messages,
    default_planner,
    parse_plan,
    plan_to_command,
)

TODAY = date(2025, 1, 15)


def _fake_planner(payload):
    """A planner that ignores the prompt and returns a fixed reply string."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda messages: text


# --- build_messages ---------------------------------------------------------


def test_build_messages_embeds_context_and_question():
    messages = build_messages("what changed at Centerfield?")
    assert set(messages) == {"system", "user"}
    assert messages["user"] == "what changed at Centerfield?"
    # The domain context (product types, license) is in the system prompt so the
    # model plans with the facts, not from memory.
    assert "GEC" in messages["system"]
    assert "product_types" in messages["system"]
    assert "JSON object" in messages["system"]


# --- parse_plan: the determinism boundary -----------------------------------


def test_parse_plan_resolves_dates_and_products():
    plan = parse_plan(
        {
            "area": "Centerfield, Utah",
            "fuzzy": True,
            "start": "2024",
            "end": "2024-05",
            "product_types": ["gec"],
            "limit": 5,
            "rationale": "named site over spring",
        },
        "q",
        today=TODAY,
    )
    assert plan.area == "Centerfield, Utah"
    assert plan.fuzzy is True
    # A bare year snaps to first/last day via the deterministic date resolver.
    assert plan.start == "2024-01-01"
    assert plan.end == "2024-05-31"
    # Product types are canonicalised to the PRODUCT_ASSETS casing.
    assert plan.product_types == ["GEC"]
    assert plan.limit == 5


def test_parse_plan_resolves_relative_dates_against_today():
    plan = parse_plan({"start": "3 months ago", "end": "today"}, "q", today=TODAY)
    assert plan.start == "2024-10-15"
    assert plan.end == "2025-01-15"


def test_parse_plan_rejects_unresolvable_date():
    # A season the deterministic resolver refuses -- the model must emit concrete
    # dates instead. The error is surfaced, not silently dropped.
    with pytest.raises(AskError, match="Unrecognized date"):
        parse_plan({"start": "last winter"}, "q", today=TODAY)


def test_parse_plan_rejects_unknown_product_type():
    with pytest.raises(AskError, match="Unknown product type"):
        parse_plan({"product_types": ["GEC", "NOPE"]}, "q", today=TODAY)


def test_parse_plan_validates_bbox_shape_and_range():
    with pytest.raises(AskError, match="min_lon"):
        parse_plan({"bbox": [1, 2, 3]}, "q", today=TODAY)
    with pytest.raises(AskError, match="out of WGS84 range"):
        parse_plan({"bbox": [0, 0, 0, 200]}, "q", today=TODAY)
    with pytest.raises(AskError, match="min must not exceed max"):
        parse_plan({"bbox": [10, 0, 5, 1]}, "q", today=TODAY)
    plan = parse_plan({"bbox": [-118.3, 33.7, -118.1, 33.8]}, "q", today=TODAY)
    assert plan.bbox == (-118.3, 33.7, -118.1, 33.8)


def test_parse_plan_place_and_bbox_are_mutually_exclusive():
    with pytest.raises(AskError, match="not both"):
        parse_plan({"place": "Tokyo", "bbox": [0, 0, 1, 1]}, "q", today=TODAY)


def test_parse_plan_rejects_start_after_end():
    with pytest.raises(AskError, match="after end"):
        parse_plan({"start": "2025-06-01", "end": "2025-01-01"}, "q", today=TODAY)


def test_parse_plan_rejects_non_positive_limit():
    with pytest.raises(AskError, match="positive"):
        parse_plan({"limit": 0}, "q", today=TODAY)


def test_parse_plan_ignores_unknown_keys_and_empty_values():
    plan = parse_plan(
        {"area": "", "place": None, "bbox": [], "surprise": "ignored", "limit": None},
        "q",
        today=TODAY,
    )
    assert plan.area is None and plan.place is None and plan.bbox is None
    assert plan.limit is None
    assert plan.product_types == []


# --- command rendering ------------------------------------------------------


def test_plan_to_command_is_a_copy_pasteable_search():
    plan = SearchPlan(
        question="q",
        area="Centerfield, Utah",
        fuzzy=True,
        start="2024-03-01",
        end="2024-05-31",
        product_types=["GEC"],
        limit=3,
    )
    cmd = plan_to_command(plan)
    assert cmd == (
        "umbra search --area 'Centerfield, Utah' --fuzzy "
        "--start 2024-03-01 --end 2024-05-31 --product GEC --limit 3"
    )


def test_plan_to_command_renders_bbox_and_max_per_task():
    plan = SearchPlan(question="q", bbox=(-118.3, 33.7, -118.1, 33.8), max_per_task=1)
    cmd = plan_to_command(plan)
    assert "--bbox -118.3,33.7,-118.1,33.8" in cmd
    assert "--max-per-task 1" in cmd


# --- ask(): end-to-end with an injected planner -----------------------------


def test_ask_extracts_json_from_a_fenced_reply():
    reply = 'Sure!\n```json\n{"area": "Provo", "rationale": "site"}\n```\nThanks'
    plan = ask("where is Provo?", planner=_fake_planner(reply), today=TODAY)
    assert plan.area == "Provo"
    assert plan.rationale == "site"
    assert plan.question == "where is Provo?"


def test_ask_extracts_json_with_surrounding_prose():
    reply = 'The plan is {"area": "Suez Canal", "limit": 2} for your request.'
    plan = ask("suez", planner=_fake_planner(reply), today=TODAY)
    assert plan.area == "Suez Canal"
    assert plan.limit == 2


def test_ask_raises_when_reply_has_no_json():
    with pytest.raises(AskError, match="did not contain a JSON object"):
        ask("q", planner=lambda m: "I cannot help with that.", today=TODAY)


def test_ask_rejects_empty_question():
    with pytest.raises(AskError, match="Ask a question"):
        ask("   ", planner=_fake_planner({"area": "x"}), today=TODAY)


# --- default_planner: provider selection from env (no network) --------------


def test_default_planner_errors_without_a_key(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MissingDependencyError, match="model API key"):
        default_planner()


def test_default_planner_prefers_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        return {"content": [{"type": "text", "text": '{"area": "X", "rationale": "r"}'}]}

    monkeypatch.setattr(planner_mod, "_post_json", fake_post)
    planner = default_planner(model="claude-test")
    text = planner({"system": "s", "user": "u"})
    assert "api.anthropic.com" in captured["url"]
    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert '"area": "X"' in text


def test_default_planner_falls_back_to_openai(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    captured = {}

    def fake_post(url, headers, payload):
        captured["url"] = url
        return {"choices": [{"message": {"content": '{"place": "Tokyo"}'}}]}

    monkeypatch.setattr(planner_mod, "_post_json", fake_post)
    planner = default_planner()
    text = planner({"system": "s", "user": "u"})
    assert captured["url"] == "https://proxy.example/v1/chat/completions"
    assert '"place": "Tokyo"' in text


# --- CLI: umbra ask ---------------------------------------------------------


@pytest.fixture
def fixed_plan(monkeypatch):
    """Point the CLI's default planner at a fixed reply, so ``umbra ask`` runs
    end-to-end without a model."""
    reply = json.dumps(
        {
            "area": "Centerfield, Utah",
            "fuzzy": True,
            "start": "2024-03-01",
            "end": "2024-05-31",
            "product_types": ["GEC"],
            "limit": 3,
            "rationale": "named site over spring",
        }
    )
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: reply)
    return reply


def test_cli_ask_shows_the_command_without_running(fixed_plan):
    result = CliRunner().invoke(cli, ["ask", "what changed at centerfield last spring?"])
    assert result.exit_code == 0, result.output
    assert "Plan: named site over spring" in result.output
    assert "umbra search --area 'Centerfield, Utah' --fuzzy" in result.output
    assert "--run to execute" in result.output


def test_cli_ask_json_emits_the_plan(fixed_plan):
    result = CliRunner().invoke(cli, ["ask", "q", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["area"] == "Centerfield, Utah"
    assert data["start"] == "2024-03-01"
    assert data["command"].startswith("umbra search")


def test_cli_ask_run_executes_the_search(fixed_plan, monkeypatch, sample_item_dict):
    item = UmbraItem.from_dict(sample_item_dict, href="https://example/item.json")

    class FakeSource:
        def __init__(self):
            self.kwargs = None

        def search(self, **kwargs):
            self.kwargs = kwargs
            return iter([item])

        def close(self):
            pass

    fake = FakeSource()
    # Route execution through a fake backend instead of a live S3 walk.
    monkeypatch.setattr("umbra_py.cli._search_source", lambda local, db_path: (fake, False))

    result = CliRunner().invoke(cli, ["ask", "q", "--run"])
    assert result.exit_code == 0, result.output
    # The plan is still shown before running, then the results follow.
    assert "umbra search --area 'Centerfield, Utah'" in result.output
    assert "1 item(s)." in result.output
    # The validated plan's filters reached the search backend.
    assert fake.kwargs["area"] == "Centerfield, Utah"
    assert fake.kwargs["fuzzy"] is True
    assert fake.kwargs["start"] == "2024-03-01"
    assert fake.kwargs["product_types"] == ["GEC"]


def test_cli_ask_limit_flag_overrides_the_plan(fixed_plan, monkeypatch):
    captured = {}

    def fake_gather(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("umbra_py.cli._gather_items", fake_gather)
    result = CliRunner().invoke(cli, ["ask", "q", "--run", "--limit", "99"])
    assert result.exit_code == 0, result.output
    assert captured["limit"] == 99


def test_cli_ask_reports_missing_key_cleanly(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = CliRunner().invoke(cli, ["ask", "q"])
    assert result.exit_code != 0
    assert "model API key" in result.output


def test_cli_ask_reports_a_bad_plan_cleanly(monkeypatch):
    monkeypatch.setattr(
        planner_mod,
        "default_planner",
        lambda **k: lambda m: '{"product_types": ["NOPE"]}',
    )
    result = CliRunner().invoke(cli, ["ask", "q"])
    assert result.exit_code != 0
    assert "Unknown product type" in result.output
