"""Post-deploy check that the hosted MCP still lists the stable tool names.

The weekly index publish redeploys the community host. ``/healthz`` being up
means the process is listening; the first ``tools/list`` on a cold container
can still take about 25 seconds. This module retries that one call. It is
stdlib only, so the publish job (which does not install the ``mcp`` extra)
can run it.

The host speaks stateless Streamable HTTP. ``tools/list`` is one POST and
needs no session id. The body is either a JSON-RPC object or an SSE
``data:`` line carrying one.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

#: The stable core. Names only: the argument contract is ``tests/mcp_contract.json``.
STABLE_CORE = (
    "search_catalog",
    "get_item",
    "quicklook",
    "download_asset",
    "geocode_place",
    "index_stats",
)

HOSTED_MCP_URL = "https://api.umbra-py.space/mcp"

#: One attempt is long enough for a ~25s cold start. Later attempts cover a
#: process that accepted ``/healthz`` and then dropped the first MCP request.
ATTEMPTS = 4
PAUSE_S = 5.0
TIMEOUT_S = 30.0

_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
    "user-agent": "umbra-py-mcp-smoke",
}

_Opener = Callable[..., Any]
_Sleeper = Callable[[float], None]


def decode_rpc(raw: str) -> dict[str, Any]:
    """The JSON-RPC object in a Streamable HTTP response body."""
    text = raw.strip()
    if not text:
        raise ValueError("MCP response was empty")
    if text.startswith("{"):
        payload = json.loads(text)
    else:
        data_lines = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not data_lines:
            raise ValueError("MCP response had no JSON body")
        payload = json.loads(data_lines[-1])
    if not isinstance(payload, dict):
        raise ValueError("MCP response was not a JSON object")
    if "error" in payload:
        raise ValueError(f"MCP error: {payload['error']}")
    return payload


def tool_names_from_rpc(payload: dict[str, Any]) -> list[str]:
    """Tool names from a ``tools/list`` result. Raises when the list is empty."""
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("tools/list returned no result")
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise ValueError("tools/list returned no tools array")
    names = [
        tool["name"]
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ]
    if not names:
        raise ValueError("tools/list returned an empty tool list")
    return names


def list_tool_names(
    url: str = HOSTED_MCP_URL,
    *,
    timeout: float = TIMEOUT_S,
    opener: _Opener = urllib.request.urlopen,
) -> list[str]:
    """POST ``tools/list`` once and return the tool names."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers=_HEADERS)
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.URLError as exc:
        raise ValueError(f"MCP request failed: {exc}") from exc
    return tool_names_from_rpc(decode_rpc(raw))


def assert_hosted_core_tools(
    url: str = HOSTED_MCP_URL,
    *,
    attempts: int = ATTEMPTS,
    pause_s: float = PAUSE_S,
    timeout: float = TIMEOUT_S,
    opener: _Opener = urllib.request.urlopen,
    sleeper: _Sleeper = time.sleep,
) -> list[str]:
    """Retry ``tools/list`` until the stable core is present.

    Returns the names the host listed. Raises ``RuntimeError`` when they are
    still missing after ``attempts``.
    """
    last_error = "no attempt"
    for attempt in range(1, attempts + 1):
        try:
            names = list_tool_names(url, timeout=timeout, opener=opener)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            names = []
        missing = [name for name in STABLE_CORE if name not in names]
        if names and not missing:
            return names
        if names:
            last_error = "missing " + ", ".join(missing)
        if attempt < attempts:
            sleeper(pause_s)
    raise RuntimeError(
        f"hosted MCP at {url} did not list the stable tools after {attempts} attempts: {last_error}"
    )
