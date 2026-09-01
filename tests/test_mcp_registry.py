"""The zero-install MCP invocation must be one command, and it must be real.

``umbra-mcp`` is one of the two AI-native front doors, and the one whose whole
pitch is that a client needs nothing installed. That pitch rests on a command,
and the command has two independent ways to rot:

* **It can be wrong.** The console script is ``umbra-mcp`` but the distribution
  is ``umbra-py``, and the server needs the ``[mcp]`` extra — so the obvious
  short form (the script name handed straight to ``uvx``) resolves to a
  distribution that does not exist. That is exactly what every surface in this
  repository documented until this suite existed.
* **It can drift.** ``server.json`` publishes the same command to the MCP
  registry, where nothing in this repository ever reads it back. A renamed
  console script, a renamed extra or a bumped version would leave the registry
  advertising a command that no longer runs, and the failure would surface as a
  client that cannot start the server rather than as a red build.

So these tests derive the command from the packaging facts — ``[project.name]``,
``[project.scripts]`` and ``[project.optional-dependencies]`` in
``pyproject.toml`` — and then require every place that states it (the README,
``llms.txt``, the module docstrings, the CLI help, and ``server.json``) to state
*that* command. Parsing is all they do, in the same spirit as
``test_workflows.py``: it needs no network, no PyPI account and no MCP client,
and it is the check that would have caught the bug that shipped.
"""

from __future__ import annotations

import inspect
import json
import re
import shlex
from pathlib import Path

import pytest

from umbra_py import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_JSON = REPO_ROOT / "server.json"
README = REPO_ROOT / "README.md"

#: The reverse-DNS name the registry knows this server by. The ``io.github.``
#: namespace is what the workflow's GitHub OIDC login grants, so the owner
#: segment must match the repository that publishes it.
SERVER_NAME = "io.github.reesehammer/umbra-mcp"

#: Files that state the invocation to a human or to a model. Every ``uvx``
#: mention in one of them is checked.
DOCUMENTED_IN = (
    "README.md",
    "llms.txt",
    "llms-full.txt",
    "docs/STRATEGY.md",
    "docs/TODO.md",
    "src/umbra_py/mcp_server.py",
    "src/umbra_py/llms_txt.py",
    "src/umbra_py/cli/explore.py",
)


def _pyproject() -> dict:
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; skipped on 3.10
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _server() -> dict:
    return json.loads(SERVER_JSON.read_text(encoding="utf-8"))


def _pypi_package(server: dict) -> dict:
    packages = [p for p in server.get("packages", []) if p.get("registryType") == "pypi"]
    assert len(packages) == 1, "server.json should publish exactly one PyPI package"
    return packages[0]


def expected_argv() -> list[str]:
    """The command, derived from the packaging rather than restated.

    ``uvx --from '<distribution>[<extra>]' <console script>``. Every assertion
    below compares against this, so renaming any of the three parts fails here
    first and points at the rename rather than at a mystery string.
    """
    project = _pyproject()["project"]
    distribution = project["name"]
    assert "mcp" in project["optional-dependencies"], "the [mcp] extra must exist"
    assert "umbra-mcp" in project["scripts"], "the umbra-mcp console script must exist"
    return ["uvx", "--from", f"{distribution}[mcp]", "umbra-mcp"]


# --- server.json -----------------------------------------------------------


def test_server_json_has_the_fields_the_registry_requires():
    server = _server()
    assert server["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    # A versioned schema URL, not a floating one: the registry validates against
    # the version the file names, so it must be pinned to be reproducible.
    assert re.search(r"/schemas/\d{4}-\d{2}-\d{2}/", server["$schema"])
    assert server["name"] == SERVER_NAME
    # The registry caps the description at 100 characters.
    assert 1 <= len(server["description"]) <= 100
    assert server["repository"] == {
        "url": "https://github.com/reesehammer/umbra-py",
        "source": "github",
    }


def test_the_server_name_matches_the_repository_that_publishes_it():
    """``io.github.<owner>/…`` is only publishable by ``<owner>``.

    The workflow authenticates with GitHub OIDC, which grants the namespace
    derived from this repository — so a name whose owner segment disagrees with
    the repository URL is rejected at publish time, a week after it merged.
    """
    server = _server()
    owner = server["repository"]["url"].removeprefix("https://github.com/").split("/")[0]
    assert server["name"].split("/")[0] == f"io.github.{owner}"


def test_server_json_version_tracks_the_package_version():
    server = _server()
    assert server["version"] == __version__
    assert _pypi_package(server)["version"] == __version__


def test_the_published_package_is_this_distribution():
    package = _pypi_package(_server())
    assert package["identifier"] == _pyproject()["project"]["name"]
    assert package["registryBaseUrl"] == "https://pypi.org"
    assert package["transport"] == {"type": "stdio"}
    assert package["runtimeHint"] == "uvx"


def test_server_json_runtime_arguments_spell_the_real_command():
    """The registry entry composes to the command the docs give.

    ``uvx`` plus the runtime arguments *is* the invocation; the extra and the
    console script are not restated here but read off ``pyproject.toml``.
    """
    package = _pypi_package(_server())
    argv = ["uvx"]
    for argument in package["runtimeArguments"]:
        if argument["type"] == "named":
            argv += [argument["name"], argument["value"]]
        else:
            argv.append(argument["value"])
    assert argv == expected_argv()


def test_appending_the_identifier_would_still_start_the_server():
    """Why the ambiguity in how a client composes the command is harmless.

    A client that renders ``uvx <runtimeArguments> <identifier>`` produces
    ``uvx --from 'umbra-py[mcp]' umbra-mcp umbra-py`` — the right script with a
    stray trailing word. That is only survivable because the entry point takes
    no arguments at all, so it is asserted rather than assumed: giving
    ``umbra-mcp`` a command line would make this entry a coin flip.
    """
    from umbra_py.mcp_server import main

    assert list(inspect.signature(main).parameters) == []


def test_umbra_mcp_help_documents_http():
    from click.testing import CliRunner

    from umbra_py.cli import cli

    result = CliRunner().invoke(cli, ["mcp", "--help"])
    assert result.exit_code == 0, result.output
    assert "--http" in result.output
    assert "Streamable HTTP" in result.output


def test_the_declared_environment_variables_are_ones_the_package_reads():
    """A declared variable a client sets and nothing consumes is a lie."""
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / "src").rglob("*.py")
    )
    for variable in _pypi_package(_server()).get("environmentVariables", []):
        assert f'"{variable["name"]}"' in sources, (
            f"server.json declares {variable['name']}, which nothing in the package reads"
        )
        # The open archive needs no credentials; everything here is opt-in.
        assert not variable.get("isRequired", False)


def test_the_readme_carries_the_registry_ownership_marker():
    """How the registry proves this repository owns the PyPI distribution.

    For a PyPI package the registry fetches the project's own long description
    and looks for an ``mcp-name:`` line naming the server. It travels in the
    README because that is what ``pyproject.toml`` ships as the description —
    which also means removing it breaks publishing rather than a test only.
    """
    assert f"mcp-name: {SERVER_NAME}" in README.read_text(encoding="utf-8")
    assert _pyproject()["project"]["readme"] == "README.md"


# --- the documented invocation ---------------------------------------------


def _documented_commands(text: str) -> list[list[str]]:
    """Every ``uvx …`` command line in ``text``, lexed.

    A mention runs to the end of its line or to the first backtick, which is
    what closes it in Markdown prose, in an ``llms.txt`` bullet and in a Python
    docstring alike. A bare ``uvx`` with nothing after it is the JSON
    ``"command": "uvx"`` form and is checked by the JSON test instead.
    """
    commands = []
    for match in re.finditer(r"\buvx\b", text):
        tail = text[match.start() :].split("\n", 1)[0].split("`", 1)[0]
        try:
            argv = shlex.split(tail.rstrip(" .,;:)\"'"))
        except ValueError:  # unbalanced quote: not a command line
            continue
        if argv != ["uvx"]:
            commands.append(argv)
    return commands


@pytest.mark.parametrize("relative_path", DOCUMENTED_IN)
def test_every_documented_uvx_command_is_the_one_that_runs(relative_path):
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for argv in _documented_commands(text):
        assert argv == expected_argv(), (
            f"{relative_path} documents `{shlex.join(argv)}`, which is not the command "
            f"that runs the server (`{shlex.join(expected_argv())}`)"
        )


def test_the_invocation_is_actually_documented_somewhere():
    """Guard the guard: a scan that finds nothing passes vacuously."""
    found = sum(
        len(_documented_commands((REPO_ROOT / path).read_text(encoding="utf-8")))
        for path in DOCUMENTED_IN
    )
    assert found >= 4, "the zero-install command should be stated on every front door"


def test_the_readme_client_configuration_blocks_are_runnable():
    """The paste-in MCP client config has to compose to the same command."""
    blocks = re.findall(r"```json\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    configured = []
    for block in blocks:
        if "uvx" not in block:
            continue
        for server in json.loads(block)["mcpServers"].values():
            configured.append([server["command"], *server.get("args", [])])
    assert configured, "the README should show a zero-install client configuration"
    for argv in configured:
        assert argv == expected_argv()


@pytest.mark.network
def test_server_json_validates_against_its_own_schema():
    """The offline tests own the project's invariants; the schema owns its own.

    Network-marked because it fetches the schema the file names. Everything the
    registry would reject for a *structural* reason is caught here; everything
    it would reject because the command is wrong is caught above.
    """
    import requests

    jsonschema = pytest.importorskip("jsonschema")

    server = _server()
    schema = requests.get(server["$schema"], timeout=30).json()
    jsonschema.validate(instance=server, schema=schema)
