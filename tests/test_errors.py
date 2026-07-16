"""Offline tests for the machine-readable error contract (AI_INTEGRATION §A1).

Covers the shape of :meth:`UmbraError.to_dict`, the optional ``hint`` field and
its propagation from a real raise site, the ``--json`` / ``UMBRA_JSON`` switch
that selects JSON-vs-prose error output, and the ``cli.main`` handler that
emits each form. Everything here is stdlib-only; no network, no extras.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from umbra_py import cli
from umbra_py.exceptions import (
    CatalogError,
    MissingDependencyError,
    UmbraError,
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "schemas" / "error.schema.json"


def test_to_dict_shape_and_class_name():
    err = CatalogError("could not read catalog", hint="check the URL")
    assert err.to_dict() == {
        "error": "CatalogError",
        "message": "could not read catalog",
        "hint": "check the URL",
    }


def test_hint_defaults_to_none_and_message_is_positional():
    # Backwards compatible: existing `raise SomeError("msg")` sites keep working,
    # str(exc) is still the message, and hint is simply absent.
    err = CatalogError("boom")
    assert str(err) == "boom"
    assert err.hint is None
    assert err.to_dict()["hint"] is None


def test_to_dict_is_json_serializable_with_null_hint():
    payload = json.loads(json.dumps(UmbraError("plain").to_dict()))
    assert payload == {"error": "UmbraError", "message": "plain", "hint": None}


def test_to_dict_matches_published_schema():
    # The committed schema is the public contract; a drift in either direction
    # (renamed key, dropped field) should fail the build.
    schema = json.loads(SCHEMA_PATH.read_text())
    keys = set(CatalogError("x", hint="y").to_dict())
    assert keys == set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_require_raises_with_hint():
    # Exercise a real raise site: viz._require on a missing module surfaces the
    # install command as a structured hint, not just prose.
    from umbra_py import viz

    with pytest.raises(MissingDependencyError) as excinfo:
        viz._require("umbra_py_no_such_module_xyz")
    assert excinfo.value.hint == 'pip install "umbra-py[viz]"'
    assert excinfo.value.to_dict()["error"] == "MissingDependencyError"


@pytest.mark.parametrize(
    ("env", "argv", "expected"),
    [
        (None, ["umbra", "search"], False),
        (None, ["umbra", "search", "--json"], True),
        ("1", ["umbra", "search"], True),
        ("true", ["umbra", "search"], True),
        ("0", ["umbra", "search"], False),
        ("false", ["umbra", "search"], False),
        ("", ["umbra", "search"], False),
    ],
)
def test_json_errors_requested(monkeypatch, env, argv, expected):
    if env is None:
        monkeypatch.delenv("UMBRA_JSON", raising=False)
    else:
        monkeypatch.setenv("UMBRA_JSON", env)
    monkeypatch.setattr(sys, "argv", argv)
    assert cli._json_errors_requested() is expected


def _force_cli_error(monkeypatch, err: UmbraError) -> None:
    """Make the Click group raise ``err`` so ``cli.main`` handles it."""

    def boom(*_args, **_kwargs):
        raise err

    monkeypatch.setattr(cli.cli, "main", boom)


def test_main_prints_prose_error_with_hint(monkeypatch, capsys):
    monkeypatch.delenv("UMBRA_JSON", raising=False)
    monkeypatch.setattr(sys, "argv", ["umbra", "search"])
    _force_cli_error(monkeypatch, CatalogError("kaboom", hint="retry"))

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "error: kaboom" in err
    assert "hint: retry" in err


def test_main_omits_hint_line_when_absent(monkeypatch, capsys):
    monkeypatch.delenv("UMBRA_JSON", raising=False)
    monkeypatch.setattr(sys, "argv", ["umbra", "search"])
    _force_cli_error(monkeypatch, CatalogError("kaboom"))

    with pytest.raises(SystemExit):
        cli.main()

    err = capsys.readouterr().err
    assert "error: kaboom" in err
    assert "hint:" not in err


def test_main_unwraps_click_exception_cause(monkeypatch, capsys):
    # Subcommands wrap domain errors as `ClickException(str(exc)) from exc`;
    # main() must recover the underlying UmbraError so hint + JSON still apply.
    import click

    monkeypatch.setenv("UMBRA_JSON", "1")
    monkeypatch.setattr(sys, "argv", ["umbra", "ask", "x"])
    original = MissingDependencyError("needs a key", hint="Set ANTHROPIC_API_KEY")
    wrapped = click.ClickException(str(original))
    wrapped.__cause__ = original
    _force_cli_error(monkeypatch, wrapped)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert json.loads(capsys.readouterr().err.strip()) == {
        "error": "MissingDependencyError",
        "message": "needs a key",
        "hint": "Set ANTHROPIC_API_KEY",
    }


def test_main_leaves_plain_usage_errors_to_click(monkeypatch, capsys):
    # A genuine usage error (no UmbraError cause) is not part of the structured
    # contract even under UMBRA_JSON -- Click renders it, with its own exit code.
    import click

    monkeypatch.setenv("UMBRA_JSON", "1")
    monkeypatch.setattr(sys, "argv", ["umbra", "search"])
    _force_cli_error(monkeypatch, click.UsageError("no such option"))

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 2  # Click's usage-error exit code
    err = capsys.readouterr().err
    assert "no such option" in err
    assert "{" not in err  # not JSON-ified


def test_main_prints_json_error_when_requested(monkeypatch, capsys):
    monkeypatch.setenv("UMBRA_JSON", "1")
    monkeypatch.setattr(sys, "argv", ["umbra", "search"])
    _force_cli_error(monkeypatch, CatalogError("kaboom", hint="retry"))

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err.strip()
    assert json.loads(err) == {
        "error": "CatalogError",
        "message": "kaboom",
        "hint": "retry",
    }
