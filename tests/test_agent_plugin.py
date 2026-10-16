"""The portable agent plugin stays a version-locked subset of umbra-mcp.

``integrations/agent-plugin/umbra-sar/`` is an opt-in plugin (MCP config plus
a short skill) for the hosted community server. Two layouts share that
folder: ``plugin.json`` + ``mcp.json`` for a folder import, and
``.claude-plugin/plugin.json`` + ``.mcp.json`` for Claude Code. The version
is copied from ``umbra_py.__version__`` so a release bump that forgets the
manifests fails here, the same way ``server.json`` cannot drift.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

from umbra_py import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN = REPO_ROOT / "integrations" / "agent-plugin" / "umbra-sar"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

HOSTED_MCP_URL = "https://api.umbra-py.space/mcp"
PLUGIN_DESCRIPTION = (
    "Search, preview and open Umbra open SAR (CC BY 4.0) via the community umbra MCP host."
)
CORE_TOOLS = [
    "search_catalog",
    "get_item",
    "quicklook",
    "download_asset",
    "geocode_place",
    "index_stats",
]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registered_tool_names() -> set[str]:
    """Names ``build_server`` passes to ``add_tool``.

    The core CI job does not install the ``mcp`` extra, so this reads the
    registration tuple instead of constructing the server. When the extra is
    present, the test below also checks those names against ``list_tools``.
    """
    tree = ast.parse((REPO_ROOT / "src" / "umbra_py" / "mcp_server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "build_server":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.For) or not isinstance(child.iter, ast.Tuple):
                continue
            if not all(isinstance(elt, ast.Name) for elt in child.iter.elts):
                continue
            names = {elt.id for elt in child.iter.elts if isinstance(elt, ast.Name)}
            if {"search_catalog", "download_asset"} <= names:
                return names
    raise AssertionError("build_server does not register its tools as a name tuple")


def test_plugin_manifests_match_package_version():
    portable = _load(PLUGIN / "plugin.json")
    claude = _load(PLUGIN / ".claude-plugin" / "plugin.json")
    assert portable == claude
    assert portable["name"] == "umbra-sar"
    assert portable["version"] == __version__
    assert portable["description"] == PLUGIN_DESCRIPTION


def test_mcp_configs_enable_only_registered_core_tools():
    portable = _load(PLUGIN / "mcp.json")
    claude = _load(PLUGIN / ".mcp.json")
    assert portable == claude
    server = portable["mcpServers"]["umbra"]
    assert server["type"] == "http"
    assert server["url"] == HOSTED_MCP_URL
    assert server["enabled_tools"] == CORE_TOOLS
    assert set(CORE_TOOLS) <= _registered_tool_names()

    if importlib.util.find_spec("mcp") is None:
        return
    import asyncio

    from umbra_py.mcp_server import build_server

    live = {tool.name for tool in asyncio.run(build_server().list_tools())}
    assert set(CORE_TOOLS) <= live
    assert _registered_tool_names() == live


def test_marketplace_lists_the_plugin():
    market = _load(MARKETPLACE)
    assert market["name"] == "umbra-py"
    assert market["owner"]["name"]
    entry = market["plugins"][0]
    assert entry == {
        "name": "umbra-sar",
        "source": "./integrations/agent-plugin/umbra-sar",
        "description": PLUGIN_DESCRIPTION,
    }
    assert (REPO_ROOT / entry["source"].removeprefix("./")).is_dir()


def test_skill_is_utf8_and_under_the_size_cap():
    skill = PLUGIN / "skills" / "umbra-sar" / "SKILL.md"
    raw = skill.read_bytes()
    assert len(raw) <= 64 * 1024
    text = raw.decode("utf-8")
    front = text.split("---", 2)[1]
    assert "name: umbra-sar" in front
    assert "搜索、预览并打开 Umbra 开放 SAR 数据（CC BY 4.0）。" in front
    assert "Contains Umbra open data, licensed under CC BY 4.0." in text
    assert "umbria" not in text.lower()
