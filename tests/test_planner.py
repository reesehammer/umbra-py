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
from umbra_py._geometry import parse_geometry
from umbra_py.cli import cli
from umbra_py.exceptions import MissingDependencyError
from umbra_py.models import UmbraItem
from umbra_py.planner import (
    AreaOfInterest,
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
    # The acquisition-property filters default to "no constraint" too.
    assert plan.polarizations == []
    assert plan.min_incidence is None
    assert plan.max_incidence is None
    assert plan.max_resolution is None


def test_parse_plan_resolves_acquisition_filters():
    plan = parse_plan(
        {
            "polarizations": ["vv", "VH", "vv"],
            "min_incidence": 20,
            "max_incidence": 45.5,
            "max_resolution": "0.5",
        },
        "q",
        today=TODAY,
    )
    # Polarizations are upper-cased and de-duplicated (an open set, not validated
    # against a fixed vocabulary -- an unknown value just matches nothing).
    assert plan.polarizations == ["VV", "VH"]
    assert plan.min_incidence == 20.0
    assert plan.max_incidence == 45.5
    # A numeric string coerces like any other number.
    assert plan.max_resolution == 0.5


def test_parse_plan_accepts_a_scalar_polarization():
    plan = parse_plan({"polarizations": "HH"}, "q", today=TODAY)
    assert plan.polarizations == ["HH"]


def test_parse_plan_rejects_non_positive_acquisition_numbers():
    for field_name in ("min_incidence", "max_incidence", "max_resolution"):
        with pytest.raises(AskError, match="positive"):
            parse_plan({field_name: 0}, "q", today=TODAY)
    with pytest.raises(AskError, match="must be a number"):
        parse_plan({"max_resolution": "fine"}, "q", today=TODAY)


def test_parse_plan_rejects_inverted_incidence_bounds():
    with pytest.raises(AskError, match="greater than max_incidence"):
        parse_plan({"min_incidence": 45, "max_incidence": 20}, "q", today=TODAY)


def test_parse_plan_rejects_malformed_polarizations():
    with pytest.raises(AskError, match="polarizations must be a list"):
        parse_plan({"polarizations": {"pol": "VV"}}, "q", today=TODAY)
    with pytest.raises(AskError, match="entries must be strings"):
        parse_plan({"polarizations": ["VV", 5]}, "q", today=TODAY)


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


def test_plan_to_command_renders_acquisition_filters():
    plan = SearchPlan(
        question="q",
        area="Centerfield, Utah",
        polarizations=["VV", "VH"],
        min_incidence=20.0,
        max_incidence=45.0,
        max_resolution=0.5,
    )
    cmd = plan_to_command(plan)
    # Each polarization is its own repeatable --pol flag (the CLI convention).
    assert "--pol VV --pol VH" in cmd
    assert "--min-incidence 20" in cmd
    assert "--max-incidence 45" in cmd
    assert "--max-resolution 0.5" in cmd


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
    monkeypatch.setattr(
        "umbra_py.cli._shared._search_source", lambda local, db_path, token=None: (fake, False)
    )

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


def test_cli_ask_run_forwards_acquisition_filters(monkeypatch, sample_item_dict):
    """A planned SAR filter (polarization/incidence/resolution) reaches the same
    search backend every other surface uses, via ``plan.to_search_kwargs()``."""
    reply = json.dumps(
        {
            "area": "Centerfield, Utah",
            "polarizations": ["VV"],
            "min_incidence": 20,
            "max_incidence": 45,
            "max_resolution": 0.5,
            "rationale": "VV scenes at low incidence",
        }
    )
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: reply)
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
    monkeypatch.setattr(
        "umbra_py.cli._shared._search_source", lambda local, db_path, token=None: (fake, False)
    )

    result = CliRunner().invoke(cli, ["ask", "q", "--run"])
    assert result.exit_code == 0, result.output
    # The command shown to the user carries the validated filters.
    assert "--pol VV" in result.output
    assert "--min-incidence 20" in result.output
    # ...and they reach the backend search as first-class keyword arguments.
    assert fake.kwargs["polarizations"] == ["VV"]
    assert fake.kwargs["min_incidence"] == 20.0
    assert fake.kwargs["max_incidence"] == 45.0
    assert fake.kwargs["max_resolution"] == 0.5


def test_cli_ask_limit_flag_overrides_the_plan(fixed_plan, monkeypatch):
    captured = {}

    def fake_gather(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", fake_gather)
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


# --- Areas of interest: chosen by name, never authored ----------------------

_DELTA_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[[-90.5, 29.0], [-89.5, 29.0], [-89.5, 29.8], [-90.5, 29.8], [-90.5, 29.0]]],
}
_RIDGE_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[[11.0, 46.0], [11.5, 46.0], [11.5, 46.4], [11.0, 46.4], [11.0, 46.0]]],
}


def _aoi(name="delta", geojson=None, source=None):
    """An :class:`AreaOfInterest` built the way the CLI builds one: geometry
    parsed by the deterministic layer *before* the model is involved."""
    return AreaOfInterest(
        name=name,
        geometry=parse_geometry(geojson or _DELTA_GEOJSON),
        source=source,
    )


def test_build_messages_omits_the_aoi_key_when_none_are_supplied():
    """A model is never offered a filter the caller cannot honour: with no areas
    supplied the prompt is exactly what it was before the feature existed."""
    assert "aoi" not in build_messages("q")["system"]


def test_build_messages_lists_supplied_areas_by_name_and_bounds():
    system = build_messages("scenes over the delta", [_aoi(), _aoi("ridge", _RIDGE_GEOJSON)])[
        "system"
    ]
    assert '"delta"' in system and '"ridge"' in system
    # The bounds distinguish two areas whose names don't; they come from the
    # user's file, so showing them costs nothing.
    assert "-90.5, 29, -89.5, 29.8" in system
    assert "1 polygon," in system
    # And the rule that makes the key safe is stated to the model.
    assert "cannot describe an area of interest yourself" in system


def test_parse_plan_resolves_a_named_area_to_the_callers_geometry():
    delta = _aoi(source="delta.geojson")
    plan = parse_plan({"aoi": "delta", "start": "2024-03-01"}, "q", today=TODAY, aois=[delta])
    assert plan.aoi is delta
    # The rings that reach the search are the caller's, not the model's.
    assert plan.to_search_kwargs()["intersects"] == delta.geometry


def test_parse_plan_matches_an_area_name_case_insensitively():
    plan = parse_plan({"aoi": " Delta "}, "q", aois=[_aoi()])
    assert plan.aoi is not None and plan.aoi.name == "delta"


def test_parse_plan_rejects_an_unknown_area_name():
    with pytest.raises(AskError, match="Unknown area of interest"):
        parse_plan({"aoi": "amazon"}, "q", aois=[_aoi()])


def test_parse_plan_rejects_an_area_when_none_were_supplied():
    """The failure mode a polygon filter exists to prevent is a silently
    unfiltered search, so a name with nothing to match is an error, not a drop."""
    with pytest.raises(AskError, match="none were supplied"):
        parse_plan({"aoi": "delta"}, "q")


def test_parse_plan_rejects_a_non_string_area():
    with pytest.raises(AskError, match="aoi must be the name"):
        parse_plan({"aoi": _DELTA_GEOJSON}, "q", aois=[_aoi()])


@pytest.mark.parametrize("extra", [{"place": "New Orleans"}, {"bbox": [-91.0, 28.0, -89.0, 30.0]}])
def test_parse_plan_rejects_an_area_combined_with_a_rectangle(extra):
    with pytest.raises(AskError, match="not more than one"):
        parse_plan({"aoi": "delta", **extra}, "q", aois=[_aoi()])


def test_parse_plan_without_an_area_leaves_intersects_unset():
    plan = parse_plan({"area": "Centerfield, Utah"}, "q", aois=[_aoi()])
    assert plan.aoi is None
    assert plan.to_search_kwargs()["intersects"] is None


def test_plan_to_command_renders_the_area_as_the_users_own_path():
    plan = SearchPlan(question="q", aoi=_aoi(source="aois/delta.geojson"))
    assert "--intersects aois/delta.geojson" in plan.to_command()


def test_plan_to_command_inlines_geojson_for_an_area_with_no_source():
    """An area built in code has no path to point at, so the audit line carries
    the geometry itself -- the printed command stays runnable either way."""
    cmd = SearchPlan(question="q", aoi=_aoi()).to_command()
    assert "--intersects" in cmd and '"Polygon"' in cmd


def test_plan_to_dict_summarises_the_area_without_the_rings():
    plan = SearchPlan(question="q", aoi=_aoi(source="delta.geojson"))
    data = json.loads(json.dumps(plan.to_dict()))  # must be JSON-serialisable
    assert data["aoi"] == {
        "name": "delta",
        "source": "delta.geojson",
        "bbox": [-90.5, 29.0, -89.5, 29.8],
    }


def test_ask_offers_the_areas_to_the_planner_and_validates_the_choice():
    seen = {}

    def planner(messages):
        seen["system"] = messages["system"]
        return json.dumps({"aoi": "delta", "rationale": "the supplied delta outline"})

    delta = _aoi(source="delta.geojson")
    plan = ask("scenes over the delta", planner=planner, today=TODAY, aois=[delta])
    assert '"delta"' in seen["system"]
    assert plan.aoi is delta


# --- CLI: umbra ask --aoi ---------------------------------------------------


@pytest.fixture
def delta_file(tmp_path):
    path = tmp_path / "delta.geojson"
    path.write_text(json.dumps(_DELTA_GEOJSON))
    return path


@pytest.fixture
def aoi_plan(monkeypatch):
    """A planner that always selects the area of interest named ``delta``."""
    reply = json.dumps({"aoi": "delta", "start": "2024-03-01", "rationale": "the delta outline"})
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: reply)
    return reply


def test_cli_ask_aoi_renders_an_intersects_command(aoi_plan, delta_file):
    result = CliRunner().invoke(cli, ["ask", "scenes over the delta", "--aoi", str(delta_file)])
    assert result.exit_code == 0, result.output
    # The file stem names the area, and the audited command points back at the file.
    assert f"--intersects {delta_file}" in result.output


def test_cli_ask_aoi_run_sends_the_polygon_to_the_backend(
    aoi_plan, delta_file, monkeypatch, sample_item_dict
):
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
    monkeypatch.setattr(
        "umbra_py.cli._shared._search_source", lambda local, db_path, token=None: (fake, False)
    )
    result = CliRunner().invoke(
        cli, ["ask", "scenes over the delta", "--run", "--aoi", str(delta_file)]
    )
    assert result.exit_code == 0, result.output
    # The exterior rings the search filters on are the ones parsed from the file.
    assert fake.kwargs["intersects"] == parse_geometry(_DELTA_GEOJSON)
    assert fake.kwargs["bbox"] is None


def test_cli_ask_aoi_accepts_an_explicit_name(monkeypatch, delta_file):
    reply = json.dumps({"aoi": "wetland"})
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: reply)
    result = CliRunner().invoke(cli, ["ask", "q", "--aoi", f"wetland={delta_file}"])
    assert result.exit_code == 0, result.output
    assert f"--intersects {delta_file}" in result.output


def test_cli_ask_aoi_refuses_two_areas_with_the_same_name(aoi_plan, delta_file, tmp_path):
    other = tmp_path / "nested" / "delta.geojson"
    other.parent.mkdir()
    other.write_text(json.dumps(_RIDGE_GEOJSON))
    result = CliRunner().invoke(cli, ["ask", "q", "--aoi", str(delta_file), "--aoi", str(other)])
    assert result.exit_code != 0
    assert "used twice" in result.output


def test_cli_ask_aoi_reports_a_bad_polygon_cleanly(aoi_plan, tmp_path):
    bad = tmp_path / "delta.geojson"
    bad.write_text('{"type": "Point", "coordinates": [0, 0]}')
    result = CliRunner().invoke(cli, ["ask", "q", "--aoi", str(bad)])
    assert result.exit_code != 0
    assert "--aoi delta" in result.output


def test_cli_ask_reports_an_unknown_planned_area_cleanly(delta_file, monkeypatch):
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: '{"aoi": "amazon"}')
    result = CliRunner().invoke(cli, ["ask", "q", "--aoi", str(delta_file)])
    assert result.exit_code != 0
    assert "Unknown area of interest" in result.output


def test_cli_ask_aoi_accepts_inline_geojson_named_by_position(monkeypatch):
    """Inline GeoJSON has no file stem to name it by, so it answers to 'aoi1' --
    and is taken whole, so an '=' inside it is never read as a NAME= prefix."""
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda m: '{"aoi": "aoi1"}')
    result = CliRunner().invoke(cli, ["ask", "q", "--aoi", json.dumps(_DELTA_GEOJSON)])
    assert result.exit_code == 0, result.output
    assert "--intersects" in result.output and "Polygon" in result.output
