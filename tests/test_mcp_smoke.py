"""Offline tests for the post-deploy ``tools/list`` smoke check.

No network. The opener and the sleeper are injected, which is also how the
retry timing stays out of the suite.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from umbra_py.mcp_smoke import (
    STABLE_CORE,
    assert_hosted_core_tools,
    decode_rpc,
    main,
    tool_names_from_rpc,
)


def _rpc(names: list[str]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": name} for name in names]}}


def _sse(payload: dict[str, Any]) -> str:
    return "event: message\ndata: " + json.dumps(payload) + "\n\n"


class _Body:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode()

    def __enter__(self) -> _Body:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener_from(responses: list[Any]):
    """An opener that yields ``responses`` in order.

    A string is an SSE body of a successful ``tools/list``. An exception
    instance is raised. Anything else is a programming error in the test.
    """
    pending = list(responses)

    def opener(request: urllib.request.Request, timeout: float = 0) -> _Body:
        body = request.data
        assert isinstance(body, bytes) and b"tools/list" in body
        assert timeout > 0
        item = pending.pop(0)
        if isinstance(item, BaseException):
            raise item
        return _Body(item)

    return opener


def test_decode_rpc_reads_sse_and_json():
    payload = _rpc(["search_catalog"])
    assert decode_rpc(_sse(payload)) == payload
    assert decode_rpc(json.dumps(payload)) == payload


def test_decode_rpc_rejects_an_error_and_an_empty_body():
    with pytest.raises(ValueError, match="MCP error"):
        decode_rpc(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}}))
    with pytest.raises(ValueError, match="empty"):
        decode_rpc("  ")
    with pytest.raises(ValueError, match="no JSON"):
        decode_rpc("event: message\n")


def test_tool_names_require_a_non_empty_list():
    assert tool_names_from_rpc(_rpc(["get_item"])) == ["get_item"]
    with pytest.raises(ValueError, match="empty"):
        tool_names_from_rpc(_rpc([]))
    with pytest.raises(ValueError, match="no result"):
        tool_names_from_rpc({"jsonrpc": "2.0", "id": 1})
    with pytest.raises(ValueError, match="no tools array"):
        tool_names_from_rpc({"jsonrpc": "2.0", "id": 1, "result": {"tools": None}})


def test_decode_rpc_rejects_a_json_array():
    with pytest.raises(ValueError, match="not a JSON object"):
        decode_rpc("data: [1]\n")


def test_assert_succeeds_without_sleeping_when_the_core_is_present():
    slept: list[float] = []
    names = assert_hosted_core_tools(
        opener=_opener_from([_sse(_rpc([*STABLE_CORE, "find_repeat_sites"]))]),
        sleeper=slept.append,
        attempts=4,
        pause_s=5,
    )
    assert set(STABLE_CORE) <= set(names)
    assert slept == []


def test_assert_retries_a_cold_start_then_accepts_the_core():
    slept: list[float] = []
    names = assert_hosted_core_tools(
        opener=_opener_from(
            [
                urllib.error.URLError("timed out"),
                _sse(_rpc(["search_catalog"])),
                _sse(_rpc(list(STABLE_CORE))),
            ]
        ),
        sleeper=slept.append,
        attempts=4,
        pause_s=5,
    )
    assert set(STABLE_CORE) <= set(names)
    assert slept == [5, 5]


def test_stable_core_is_the_contract_snapshot_keys():
    """One list: ``umbra_py.mcp_smoke.STABLE_CORE`` and the snapshot keys."""
    snapshot = json.loads((Path(__file__).parent / "mcp_contract.json").read_text(encoding="utf-8"))
    assert tuple(snapshot) == STABLE_CORE


def test_main_reports_a_missing_core_name(monkeypatch, capsys):
    def boom(**kwargs: object) -> list[str]:
        raise RuntimeError("missing quicklook")

    monkeypatch.setattr("umbra_py.mcp_smoke.assert_hosted_core_tools", boom)
    assert main() == 1
    captured = capsys.readouterr()
    assert "::error title=Hosted MCP tools::missing quicklook" in captured.out


def test_main_prints_the_core_when_the_probe_succeeds(monkeypatch, capsys):
    monkeypatch.setattr(
        "umbra_py.mcp_smoke.assert_hosted_core_tools",
        lambda **kwargs: list(STABLE_CORE),
    )
    assert main() == 0
    assert "search_catalog" in capsys.readouterr().out


def test_assert_fails_when_a_stable_name_never_appears():
    slept: list[float] = []
    present = [name for name in STABLE_CORE if name != "quicklook"]
    with pytest.raises(RuntimeError, match="missing quicklook"):
        assert_hosted_core_tools(
            opener=_opener_from([_sse(_rpc(present))] * 3),
            sleeper=slept.append,
            attempts=3,
            pause_s=5,
        )
    assert slept == [5, 5]
