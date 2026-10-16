"""Stability contract for the MCP tools a client may pin.

``tests/mcp_contract.json`` records, for each stable-core tool, the name, the
``required`` argument list, and each argument's JSON type. Building the server
and comparing it to that snapshot fails when a tool disappears, an argument
disappears or changes type, or an argument becomes required. A new optional
argument is compatible, and still fails until the snapshot is updated, so the
addition is reviewed.

The module is skipped where the ``mcp`` extra is not installed, matching
``tests/test_mcp_server.py``. The core CI job never sees it; the all-extras
job does.

``search_catalog`` and ``get_item`` keep the SDK's loose object ``outputSchema``.
``search_catalog`` returns a wrapper (``count``, ``items``, …), not an
item-context card, so advertising ``item-context.schema.json`` as that tool's
output schema would make a client that validates structured output reject
every search. ``get_item`` does return a card, and replacing the loose schema
clients already receive with the strict closed schema is a wire change this
contract does not take. The card test below checks that the tool results
still validate against the published schema.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest
import responses

pytest.importorskip("mcp")

from umbra_py import __version__  # noqa: E402
from umbra_py import mcp_server as ms  # noqa: E402
from umbra_py.mcp_smoke import STABLE_CORE  # noqa: E402
from umbra_py.models import UmbraItem  # noqa: E402

CONTRACT_PATH = Path(__file__).parent / "mcp_contract.json"


def _json_type(schema: dict[str, Any]) -> str:
    """A stable spelling of one argument's JSON Schema type.

    Optional arguments arrive as ``anyOf`` (the type, plus ``null``). Array
    arguments keep their item type, so ``list[str]`` and ``list[float]`` do
    not compare equal. Union member order is not part of the type. A shape
    this does not spell becomes ``unknown``, and the snapshot diff fails.
    """
    if "anyOf" in schema:
        return "|".join(sorted(_json_type(part) for part in schema["anyOf"]))
    type_name = schema.get("type")
    if type_name == "array":
        items = schema.get("items")
        if isinstance(items, dict) and items:
            return f"array[{_json_type(items)}]"
        return "array"
    if isinstance(type_name, str):
        return type_name
    return "unknown"


def _tool_contract(tool: Any) -> dict[str, Any]:
    schema = tool.input_schema
    properties = schema.get("properties") or {}
    return {
        "required": list(schema.get("required") or []),
        "args": {name: _json_type(prop) for name, prop in properties.items()},
    }


def _live_contract() -> dict[str, Any]:
    server = ms.build_server()
    tools = asyncio.run(server.list_tools())
    return {tool.name: _tool_contract(tool) for tool in tools}


def _snapshot() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _contract_errors(live: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """How ``live`` breaks the frozen ``snapshot``. Empty means it holds."""
    errors: list[str] = []
    for name, spec in snapshot.items():
        if name not in live:
            errors.append(f"{name}: stable tool was removed")
            continue
        got = live[name]
        snap_args: dict[str, str] = spec["args"]
        live_args: dict[str, str] = got["args"]
        for arg, typ in snap_args.items():
            if arg not in live_args:
                errors.append(f"{name}.{arg}: argument was removed")
            elif live_args[arg] != typ:
                errors.append(f"{name}.{arg}: type changed from {typ!r} to {live_args[arg]!r}")
        for arg in live_args:
            if arg not in snap_args:
                errors.append(f"{name}.{arg}: new argument; update tests/mcp_contract.json")
        snap_required = set(spec["required"])
        live_required = set(got["required"])
        for arg in sorted(live_required - snap_required):
            errors.append(f"{name}.{arg}: newly required")
        for arg in sorted(snap_required - live_required):
            errors.append(f"{name}.{arg}: no longer required; update tests/mcp_contract.json")
    return errors


def test_snapshot_names_the_stable_core():
    """The snapshot keys are ``umbra_py.mcp_smoke.STABLE_CORE``, in that order."""
    assert tuple(_snapshot()) == STABLE_CORE


def test_live_server_matches_the_contract_snapshot():
    errors = _contract_errors(_live_contract(), _snapshot())
    assert errors == [], "\n".join(errors)


def test_a_removed_tool_fails_the_contract():
    live = _live_contract()
    del live["search_catalog"]
    errors = _contract_errors(live, _snapshot())
    assert "search_catalog: stable tool was removed" in errors


def test_a_removed_or_retyped_argument_fails_the_contract():
    live = _live_contract()
    removed = copy.deepcopy(live)
    del removed["get_item"]["args"]["url"]
    assert "get_item.url: argument was removed" in _contract_errors(removed, _snapshot())

    retyped = copy.deepcopy(live)
    retyped["quicklook"]["args"]["max_size"] = "string"
    errors = _contract_errors(retyped, _snapshot())
    assert any(error.startswith("quicklook.max_size: type changed") for error in errors)


def test_a_newly_required_argument_fails_the_contract():
    live = copy.deepcopy(_live_contract())
    live["download_asset"]["required"] = ["url", "confirm"]
    errors = _contract_errors(live, _snapshot())
    assert "download_asset.confirm: newly required" in errors


def test_a_new_argument_requires_a_snapshot_update():
    live = copy.deepcopy(_live_contract())
    live["geocode_place"]["args"]["limit"] = "integer"
    errors = _contract_errors(live, _snapshot())
    assert "geocode_place.limit: new argument; update tests/mcp_contract.json" in errors


def test_server_version_is_the_package_version():
    server = ms.build_server()
    assert server.version == __version__
    assert server.version != ""


def test_read_only_hint_is_set_only_on_tools_that_do_not_write():
    """``download_asset`` writes a file. ``watch_site`` persists a baseline.

    Every other registered tool only reads, and says so. A new tool has to
    be classified here before the suite stays green.
    """
    server = ms.build_server()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == ms._READ_ONLY_TOOLS | {"download_asset", "watch_site"}
    for name in ms._READ_ONLY_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None and annotations.read_only_hint is True, name
        wire = tools[name].model_dump(by_alias=True, exclude_none=True)
        assert wire["annotations"]["readOnlyHint"] is True
    for name in ("download_asset", "watch_site"):
        annotations = tools[name].annotations
        assert annotations is None or annotations.read_only_hint is not True


@responses.activate
def test_tool_cards_validate_against_item_context(monkeypatch):
    """The cards ``get_item`` and ``search_catalog`` return match the schema.

    This is the check that stays in place of wiring that schema in as
    ``outputSchema`` (see the module docstring).
    """
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = (
        Path(__file__).resolve().parents[1] / "docs" / "schemas" / "item-context.schema.json"
    )
    validator = jsonschema.Draft202012Validator(json.loads(schema_path.read_text()))
    sample_item_dict = json.loads((Path(__file__).parent / "data" / "sample_item.json").read_text())
    item_url = "https://umbra-open-data-catalog.s3.amazonaws.com/x/item.stac.v2.json"
    responses.add(responses.GET, item_url, json=sample_item_dict, status=200)

    card = ms.get_item(item_url)
    errors = list(validator.iter_errors(card))
    assert not errors, errors

    item = UmbraItem.from_dict(sample_item_dict, href=item_url)

    class _FakeCatalog:
        def search(self, **kwargs: object) -> Any:
            return iter([item])

    monkeypatch.setattr(ms, "UmbraCatalog", lambda *a, **k: _FakeCatalog())
    found = ms.search_catalog(limit=1, local=False)
    for entry in found["items"]:
        errors = list(validator.iter_errors(entry))
        assert not errors, errors
