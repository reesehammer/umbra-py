"""Every ``umbra ...`` command *and* inline ``python -c`` body in a GitHub
Actions workflow must still resolve against the code it names.

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

The CLI half parses each invocation against the real Click command tree, which
catches exactly that class of drift at the same time as the rename that causes
it. The Python half covers the same publish pipeline's *other* moving part: the
tiling step continues into ``python -c "import umbra_py.pmtiles as p, ...;
p.save_viewer(c.CATALOG_INDEX_PMTILES_URL, ...)"``, so a renamed module, a moved
function or a retired constant breaks that weekly run just as invisibly as the
option rename did -- and until now no test would have noticed. That body is now
compiled (so a syntax slip fails a PR) and every name it reads out of
``umbra_py`` is resolved against the live package.

Both halves need no network, no credentials and no bucket crawl -- which is the
whole point: they are the checks that could have caught the failure that shipped.
"""

import ast
import importlib
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

#: Interpreters whose ``-c`` argument is an inline Python program to check.
PYTHON = frozenset({"python", "python3"})

#: The package whose names a ``python -c`` body is checked to still resolve.
PACKAGE = "umbra_py"

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


# --- The Python half: `python -c "..."` inline bodies -----------------------
#
# The publish pipeline does not stop at `umbra` calls. The tiling step tiles the
# basemap and then writes its standalone viewer from a `python -c` one-liner that
# imports library names by hand (`umbra_py.pmtiles.save_viewer`,
# `constants.CATALOG_INDEX_PMTILES_URL`). Renaming either breaks the same weekly
# run the CLI checks above guard -- and a Click parse cannot see it, because it
# is not a CLI call. So the body is compiled and its `umbra_py` names resolved.


def _python_bodies(script: str) -> list[str]:
    """Every ``python -c "<body>"`` inline program in one ``run:`` block.

    Split on newlines only -- never on ``;`` / ``|`` the way :func:`_commands`
    does -- because a ``-c`` body legitimately contains both (``import sys,json;
    ...``), and splitting on them would tear the very Python this checks apart.
    Backslash continuations are folded first so a body wrapped across lines (the
    tiling step's) is read whole. A line that will not lex is skipped, same as on
    the CLI side.
    """
    bodies = []
    for expanded in _substitutions(script.replace("\\\n", " ")):
        for line in expanded.split("\n"):
            try:
                tokens = shlex.split(line)
            except ValueError:  # unbalanced quotes: not a command we can read
                continue
            for index, token in enumerate(tokens):
                if token in PYTHON:
                    rest = tokens[index + 1 :]
                    if "-c" in rest:
                        flag = rest.index("-c")
                        if flag + 1 < len(rest):
                            bodies.append(rest[flag + 1])
    return bodies


def _python_invocations():
    """``(workflow, step, body)`` for every ``python -c`` body in every workflow."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for body in _python_bodies(step.get("run") or ""):
                    yield path.name, step.get("name", "<unnamed>"), body


PYTHON_INVOCATIONS = list(_python_invocations())


def _package_references(body: str):
    """Names a ``python -c`` body reads out of :data:`PACKAGE`.

    Returns ``(imports, attributes)``. ``imports`` is ``(module, name)`` pairs to
    resolve -- ``name`` is ``None`` for ``import umbra_py.x`` and the imported
    member for ``from umbra_py.x import y``. ``attributes`` is ``(module, attr)``
    pairs read off a module alias, i.e. the ``p.save_viewer`` in ``import
    umbra_py.pmtiles as p``. Together they cover both shapes a body can name a
    library symbol by. :func:`ast.parse` raises :class:`SyntaxError` on drift the
    resolution never gets to.
    """
    tree = ast.parse(body)
    aliases: dict[str, str] = {}  # local name -> umbra_py module it aliases
    imports: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE or alias.name.startswith(PACKAGE + "."):
                    imports.append((alias.name, None))
                    if alias.asname:  # `import umbra_py.pmtiles as p`
                        aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == PACKAGE or module.startswith(PACKAGE + "."):
                for alias in node.names:
                    imports.append((module, alias.name))
    attributes = [
        (aliases[node.value.id], node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
    ]
    return imports, attributes


@pytest.mark.parametrize(
    ("workflow", "step", "body"),
    PYTHON_INVOCATIONS,
    ids=[f"{w}:{s}" for w, s, _ in PYTHON_INVOCATIONS],
)
def test_workflow_python_body_resolves(workflow, step, body):
    """Each ``python -c`` body compiles and every ``umbra_py`` name it reads exists."""
    where = f"{workflow} step {step!r}: `python -c` body"
    try:
        imports, attributes = _package_references(body)
    except SyntaxError as error:
        pytest.fail(f"{where} does not compile: {error}")
    for module_name, member in imports:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:  # renamed or moved module
            pytest.fail(f"{where} imports {module_name!r}, which does not import: {error}")
        if member is not None and not hasattr(module, member):
            pytest.fail(f"{where} imports {member!r} from {module_name}, which has no such name")
    for module_name, attr in attributes:
        module = importlib.import_module(module_name)  # already resolved above
        if not hasattr(module, attr):
            pytest.fail(f"{where} reads {module_name}.{attr}, which does not exist")


def test_the_python_scan_found_the_publish_viewer_call():
    """A scanner that silently matches nothing would pass forever.

    The tiling step's viewer write is the reason this half exists, so pin that
    its body was scanned and that the two names it reads by hand were seen -- the
    ``import ... as p`` module alias and the ``constants`` member it references.
    """
    references = [_package_references(body) for _, _, body in PYTHON_INVOCATIONS]
    imports = {pair for refs, _ in references for pair in refs}
    attributes = {pair for _, attrs in references for pair in attrs}
    assert ("umbra_py.pmtiles", None) in imports, imports
    assert ("umbra_py.pmtiles", "save_viewer") in attributes, attributes
    assert ("umbra_py.constants", "CATALOG_INDEX_PMTILES_URL") in attributes, attributes


def test_a_renamed_library_name_in_a_python_body_would_be_caught():
    """The Python-half analogue of the option-rename drift, asserted not trusted."""
    body = "import umbra_py.pmtiles as p; p.save_the_viewer('catalog.html')"
    _, attributes = _package_references(body)
    module = importlib.import_module("umbra_py.pmtiles")
    assert ("umbra_py.pmtiles", "save_the_viewer") in attributes
    assert not hasattr(module, "save_the_viewer")


def test_a_retired_constant_in_a_python_body_would_be_caught():
    """A ``from umbra_py.constants import <gone>`` fails the resolve, not the run."""
    body = "from umbra_py.constants import CATALOG_INDEX_URL_THAT_WENT_AWAY"
    imports, _ = _package_references(body)
    module = importlib.import_module("umbra_py.constants")
    assert ("umbra_py.constants", "CATALOG_INDEX_URL_THAT_WENT_AWAY") in imports
    assert not hasattr(module, "CATALOG_INDEX_URL_THAT_WENT_AWAY")
