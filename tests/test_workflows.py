"""Every ``umbra ...`` command in a GitHub Actions workflow must parse.

The workflows in ``.github/workflows/`` are the only callers of the CLI that
nothing else exercises: ``ci.yml`` runs on every push, but ``publish-index.yml``
runs *weekly*, and ``docs.yml``'s showcase step only on ``main``. So a renamed
or dropped option drifts out of sync silently and is discovered a week later --
by which time the run it broke has already thrown away its crawl.

That is not hypothetical. Both of the first two ``Publish catalog index`` runs
died on ``umbra tiles --local --db catalog.db``: ``umbra tiles`` spells that
option ``--index-db`` (``--db`` means the decibel stretch on render commands),
so the whole-catalog basemap never built, the rolling ``catalog-index`` release
was never created, and every artifact the project publishes -- ``umbra index
fetch``'s ``catalog.db``, the stac-geoparquet export, ``catalog.pmtiles``, the
GitHub Pages showcase built from them -- 404'd for its entire existence.

These tests parse each invocation against the real Click command tree, which
catches exactly that class of drift at the same time as the rename that causes
it. Parsing is deliberately all they do: it is the check that needs no network,
no credentials and no bucket crawl, and it is the one that failed.
"""

import re
import shlex
from pathlib import Path

import click
import pytest
import yaml

from umbra_py.cli import cli

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Names the console scripts are installed under (see ``[project.scripts]``).
ENTRY_POINTS = frozenset({"umbra", "umbra-py"})

#: Shell operators that end one command and begin the next. Splitting on these
#: is not a shell parser -- it does not need to be. Anything it mangles fails
#: to lex and is dropped, and the only fragments that matter are the ones that
#: still begin with an entry-point name.
_SEPARATORS = re.compile(r"\|\||&&|\||;|\n")


def _substitutions(script: str) -> list[str]:
    """Split ``script`` into itself plus the body of every ``$(...)``.

    Command substitutions hold real invocations -- ``summary="$(umbra index
    info --db catalog.db)"`` is how the release notes are built -- so they are
    lifted out and checked as scripts in their own right rather than lexed as
    part of the string that quotes them. Each is replaced by a bare word in the
    parent so the quoting around it stays balanced.
    """
    scripts, out, index = [], [], 0
    while index < len(script):
        if script.startswith("$(", index):
            depth, end = 1, index + 2
            while end < len(script) and depth:
                depth += {"(": 1, ")": -1}.get(script[end], 0)
                end += 1
            out.append(" SUBSTITUTION ")
            scripts.extend(_substitutions(script[index + 2 : end - 1]))
            index = end
        else:
            out.append(script[index])
            index += 1
    return ["".join(out), *scripts]


def _commands(script: str) -> list[list[str]]:
    """Every CLI invocation in one ``run:`` block, as argv token lists."""
    found = []
    for expanded in _substitutions(script.replace("\\\n", " ")):
        for fragment in _SEPARATORS.split(expanded):
            try:
                tokens = shlex.split(fragment)
            except ValueError:  # unbalanced quotes: not a command we can read
                continue
            if tokens and tokens[0] in ENTRY_POINTS:
                found.append(tokens)
    return found


def _invocations():
    """``(workflow, step, argv)`` for every CLI call in every workflow."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for tokens in _commands(step.get("run") or ""):
                    yield path.name, step.get("name", "<unnamed>"), tokens


INVOCATIONS = list(_invocations())


def _resolve(argv):
    """Walk the subcommand path of ``argv``, returning ``(command, args, path)``.

    ``path`` is the full verb, so ``umbra index build`` is distinguishable from
    the ``umbra index`` group it hangs off.
    """
    command, args, path = cli, list(argv[1:]), [argv[0]]
    while isinstance(command, click.Group) and args and not args[0].startswith("-"):
        context = click.Context(command, info_name=command.name)
        subcommand = command.get_command(context, args[0])
        if subcommand is None:
            raise AssertionError(f"no such command: {' '.join([*path, args[0]])}")
        command, args, path = subcommand, args[1:], [*path, args[0]]
    return command, args, path


@pytest.mark.parametrize(
    ("workflow", "step", "argv"),
    INVOCATIONS,
    ids=[f"{w}:{' '.join(a[:3])}" for w, _, a in INVOCATIONS],
)
def test_workflow_command_parses(workflow, step, argv):
    """Each workflow invocation resolves to a command that accepts its options."""
    command, args, _ = _resolve(argv)
    # The option parser, not `make_context`: it validates the option *surface*
    # -- names, and how many values each takes -- without converting types or
    # demanding required values that only exist on the runner.
    parser = command.make_parser(click.Context(command, info_name=command.name))
    try:
        parser.parse_args(args)
    except click.UsageError as error:
        pytest.fail(f"{workflow} step {step!r}: `{' '.join(argv)}` -> {error.format_message()}")


def test_the_scan_actually_found_the_published_commands():
    """A scanner that silently matches nothing would pass forever.

    The publish pipeline is the reason these tests exist, so pin that its
    steps are among what was scanned -- including the two shapes the extractor
    has to work for: a line continued with a backslash (``umbra showcase``) and
    one nested in a ``$(...)`` substitution (``umbra index info``).
    """
    scanned = {" ".join(_resolve(argv)[2]) for _, _, argv in INVOCATIONS}
    assert {
        "umbra index build",
        "umbra index export",
        "umbra index info",
        "umbra tiles",
        "umbra showcase",
    } <= scanned, scanned


def test_the_drift_that_broke_the_publish_would_be_caught():
    """The exact failure this suite exists for, asserted rather than trusted."""
    command, args, _ = _resolve(["umbra", "tiles", "--local", "--db", "catalog.db"])
    parser = command.make_parser(click.Context(command, info_name=command.name))
    with pytest.raises(click.NoSuchOption):
        parser.parse_args(args)
