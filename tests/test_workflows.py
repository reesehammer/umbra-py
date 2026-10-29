"""Every ``umbra ...`` command in a GitHub Actions workflow must parse, and
every ``python -c`` body must compile and name things that still exist.

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

The ``umbra ...`` invocations are parsed against the real Click command tree,
which catches exactly that class of drift at the same time as the rename that
causes it. Parsing is deliberately all they do: it is the check that needs no
network, no credentials and no bucket crawl, and it is the one that failed.

The same publish pipeline also drives the CLI from Python: the tiling step ends
with ``python -c "import umbra_py.pmtiles as p, umbra_py.constants as c;
p.save_viewer(c.CATALOG_INDEX_PMTILES_URL, ...)"``, which references two library
symbols *by name*. A rename of ``save_viewer`` or a move of
``CATALOG_INDEX_PMTILES_URL`` would break the weekly run exactly as the option
typo did, and the Click parse above -- which only sees ``umbra`` argv -- cannot
see it. So the ``python -c`` bodies are extracted too, compiled (a syntax error
is drift), and every name they read from ``umbra_py`` is resolved against the
installed package. That is the same "check rather than restate" the option scan
is, one interpreter over. It stays offline: it imports only the ``umbra_py``
modules the snippets name (all stdlib-only today), and an import that fails for
want of an *optional* dependency is treated as an absent extra in this
environment, not as drift.
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


def _step_positions(steps: list[dict]) -> dict[str, int]:
    position = {}
    for i, step in enumerate(steps):
        if step.get("id"):
            position[step["id"]] = i
        if step.get("name"):
            position[step["name"]] = i
    return position


def test_publish_redeploys_after_both_index_and_thumbnails_are_released():
    """The hosted API boots onto whatever is released when it is redeployed.

    A redeploy before the thumbnail sidecar upload booted last week's previews;
    one gated on the token alone was a silent skip; a confirm that compared only
    the acquisition count passed against a stale API whenever a week added
    nothing. Pin the order, the gating and the snapshot comparison.

    The release job holds ``contents: write`` and never sees ``RAILWAY_TOKEN``.
    The deploy job holds the token and ``contents: read``, and it starts only
    after the publish job — including the thumbnail upload — has finished.
    Scene embeddings are a step in that same publish job. An empty key exits
    before install; the OpenAI variables exist only on that step.
    """
    document = yaml.safe_load((WORKFLOW_DIR / "publish-index.yml").read_text())
    publish_job = document["jobs"]["publish"]
    deploy_job = document["jobs"]["deploy"]
    steps = publish_job["steps"]
    deploy_steps = deploy_job["steps"]
    position = _step_positions(steps)
    deploy_position = _step_positions(deploy_steps)

    release_order = [
        position[key] for key in ("publish", THUMBS_STEP, VERIFY_STEP, EMBED_STEP, COVERAGE_STEP)
    ]
    assert release_order == sorted(release_order), release_order
    deploy_order = [deploy_position[key] for key in ("redeploy", CONFIRM_STEP, MCP_SMOKE_STEP)]
    assert deploy_order == sorted(deploy_order), deploy_order
    assert deploy_job["needs"] == "publish"
    assert "embed" not in document["jobs"]
    assert "embed_index" not in publish_job["outputs"]

    redeploy = deploy_steps[deploy_position["redeploy"]]
    confirm = deploy_steps[deploy_position[CONFIRM_STEP]]
    assert "needs.publish.outputs.published == 'success'" in redeploy["if"]
    assert "!cancelled()" in redeploy["if"] and "RAILWAY_TOKEN" not in redeploy["if"]
    assert "::warning" in redeploy["run"] and "GITHUB_STEP_SUMMARY" in redeploy["run"]
    assert "@railway/cli@5.71.0" in redeploy["run"]
    assert "@latest" not in redeploy["run"]
    assert redeploy["env"]["RAILWAY_TOKEN"] == "${{ secrets.RAILWAY_TOKEN }}"
    assert "needs.publish.outputs.etag" in confirm["env"]["WANT_SNAPSHOT"]
    assert "needs.publish.outputs.ids" in confirm["env"]["WANT_IDS"]
    assert confirm["env"]["RAILWAY_TOKEN"] == "${{ secrets.RAILWAY_TOKEN }}"
    assert "'snapshot'" in confirm["run"]
    assert "published_thumbs_snapshot" in confirm["run"]
    assert confirm.get("id") == "confirm"
    smoke = deploy_steps[deploy_position[MCP_SMOKE_STEP]]
    assert "steps.confirm.outcome == 'success'" in smoke["if"]
    assert "steps.redeploy.outputs.token == 'true'" in smoke["if"]
    assert "steps.redeploy.outputs.token == 'true'" in confirm["if"]
    assert "token=true" in redeploy["run"] and "token=false" in redeploy["run"]
    assert "env" not in smoke
    assert "assert_hosted_core_tools" in smoke["run"]
    gate = steps[position[COVERAGE_STEP]]
    assert "if" not in gate
    assert "mcp_smoke" not in gate["run"]

    thumbs, verify = steps[position[THUMBS_STEP]], steps[position[VERIFY_STEP]]
    assert "continue-on-error" not in thumbs
    assert "continue-on-error" not in verify
    assert "!cancelled()" in thumbs["if"] and "!cancelled()" in verify["if"]
    assert "steps.thumbs.outputs.publish == 'yes'" in verify["if"]
    assert "pack_thumbnail_release" in thumbs["run"]
    assert "publish_thumbnail_release" in thumbs["run"]
    assert "thumbnail_publish_line" in thumbs["run"]
    assert "--clobber" not in thumbs["run"]
    assert "assert_remote_thumbnail_release" in verify["run"]
    bake = steps[position[BAKE_STEP]]
    assert "fetch-thumbnails" in bake["run"]
    assert "--from catalog.thumbs.seed.db" in bake["run"]
    assert "catalog.thumbs.seed.merged" in bake["run"]
    assert "--limit 2400" in bake["run"]
    assert "|| true" not in bake["run"]
    assert "::warning" in bake["run"]
    workflow = (WORKFLOW_DIR / "publish-index.yml").read_text(encoding="utf-8")
    assert "catalog.thumbs.db --clobber" not in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert re.search(r"actions/cache/restore@[0-9a-f]{40} # v6\.\d+\.\d+", workflow)
    assert re.search(r"actions/cache/save@[0-9a-f]{40} # v6\.\d+\.\d+", workflow)
    assert "--validation index-validation.json" in workflow
    assert "indexed_preview_count" in workflow
    assert document["permissions"] == {"contents": "read"}
    assert publish_job["permissions"]["contents"] == "write"
    assert publish_job["permissions"]["actions"] == "write"
    assert deploy_job["permissions"] == {"contents": "read"}
    # A step `if` cannot read secrets. The embed step exits 0 when the key is
    # empty, and it is the only publish step that sees the OpenAI variables.
    # The Railway token is absent entirely: this job can edit the release.
    assert "env" not in publish_job
    embed = steps[position[EMBED_STEP]]
    assert embed.get("continue-on-error") is True
    assert "if" not in embed
    assert embed["env"] == {
        "OPENAI_API_KEY": "${{ secrets.OPENAI_API_KEY }}",
        "OPENAI_BASE_URL": "${{ secrets.OPENAI_BASE_URL }}",
        "UMBRA_SCENE_EMBED_MODEL": "${{ secrets.UMBRA_SCENE_EMBED_MODEL }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    assert "OPENAI_API_KEY is not set; skipping scene embeddings." in embed["run"]
    assert "exit 0" in embed["run"]
    assert 'pip install -e ".[ai,viz]"' in embed["run"]
    assert "gh release upload catalog-index catalog.embed.db --clobber" in embed["run"]
    for step in steps:
        if step.get("name") == EMBED_STEP:
            continue
        env = step.get("env") or {}
        assert "RAILWAY_TOKEN" not in env
        assert "OPENAI_API_KEY" not in env
        assert "OPENAI_BASE_URL" not in env
        assert "UMBRA_SCENE_EMBED_MODEL" not in env
    assert "RAILWAY_TOKEN" not in yaml.dump(publish_job)
    assert "catalog-db" not in workflow
    assert "embed-key" not in workflow
    assert "OPENAI_API_KEY" not in yaml.dump(deploy_job)


THUMBS_STEP = "Publish the thumbnail sidecar"
BAKE_STEP = "Bake SAR thumbnails (incremental, non-blocking)"
VERIFY_STEP = "Verify the thumbnail release assets"
EMBED_STEP = "Build and publish scene embeddings (opt-in, needs OPENAI_API_KEY)"
CONFIRM_STEP = "Confirm the hosted API serves this snapshot (needs RAILWAY_TOKEN)"
COVERAGE_STEP = "Coverage gate (99% of acquisitions with a sidecar)"
MCP_SMOKE_STEP = "Confirm the hosted MCP lists the stable tools"


_ACTION_PIN = re.compile(
    r"^(\s*)-\s+uses:\s+(\S+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$"
    r"|^\s*uses:\s+(\S+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$"
)


def test_third_party_actions_are_pinned_to_commits():
    """Every ``uses:`` is a full commit SHA with the release tag in a comment.

    A moving tag (``@v7``, ``@release/v1``) can be repointed. Dependabot updates
    the SHA from the ``# vX.Y.Z`` comment, so the comment has to stay.
    """
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "uses:" not in line:
                continue
            if _ACTION_PIN.match(line) is None:
                pytest.fail(f"{path.name}:{lineno} is not a SHA pin with a version comment: {line}")


def test_workflows_without_a_token_read_the_repo_only():
    """ci, docker, and docs declare contents: read at the top level."""
    for name in ("ci.yml", "docker.yml", "docs.yml"):
        document = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
        assert document["permissions"] == {"contents": "read"}, name


def test_docs_showcase_keys_stay_on_the_narrate_step():
    """Index and basemap download run with no model keys."""
    document = yaml.safe_load((WORKFLOW_DIR / "docs.yml").read_text(encoding="utf-8"))
    keyed = []
    for step in document["jobs"]["build"]["steps"]:
        env = step.get("env") or {}
        secret_names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")
        if any(name in env for name in secret_names):
            keyed.append(step["name"])
    assert keyed == ["Build catalog showcase"]
    fetch = next(
        step
        for step in document["jobs"]["build"]["steps"]
        if step.get("name") == "Fetch the catalog index and basemap"
    )
    assert "env" not in fetch
    assert "umbra index fetch" in fetch["run"]
    assert "umbra tiles --fetch" in fetch["run"]


def test_mcp_publisher_is_a_fixed_checksummed_release():
    text = (WORKFLOW_DIR / "release.yml").read_text(encoding="utf-8")
    assert "releases/latest" not in text
    assert 'version="1.8.6"' in text
    assert "1fbe93fc0783c2394d8012c8d6e50a59e98cb2cc2d216264cc69f405d0e21340" in text
    assert "sha256sum -c -" in text
    assert "registry_${version}_checksums.txt" in text
    # The tag/version check that gates PyPI upload is unchanged in spirit:
    # the built version still has to equal the release tag.
    assert 'tag="${GITHUB_REF_NAME#v}"' in text
    assert '"$pkg_version" != "$tag"' in text


def test_workflows_pin_ubuntu_24_04():
    """GitHub starts moving ubuntu-latest to Ubuntu 26 on 2026-10-19.

    Every workflow job names ubuntu-24.04 so that migration does not change
    the runner under a weekly publish or a pull-request build.
    """
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "ubuntu-latest" not in text, path.name
        document = yaml.safe_load(text)
        for job_name, job in (document.get("jobs") or {}).items():
            assert job.get("runs-on") == "ubuntu-24.04", f"{path.name}:{job_name}"


def test_the_drift_that_broke_the_publish_would_be_caught():
    """The exact failure this suite exists for, asserted rather than trusted."""
    command, args, _ = _resolve(["umbra", "tiles", "--local", "--db", "catalog.db"])
    parser = command.make_parser(click.Context(command, info_name=command.name))
    with pytest.raises(click.NoSuchOption):
        parser.parse_args(args)


# --- python -c bodies -------------------------------------------------------
#
# The publish pipeline drives the library from Python as well as from the CLI,
# and those snippets name library symbols as strings the Click parse cannot see.

#: The interpreter names a workflow spells ``python -c`` under.
_PYTHON = frozenset({"python", "python3"})


def _python_snippets(script: str) -> list[str]:
    """The body of every ``python -c '...'`` in one ``run:`` block.

    Unlike :func:`_commands`, this must respect quoting *before* splitting -- a
    snippet's body routinely contains ``;`` and ``|``, which are shell operators
    only outside the quotes that hold the ``-c`` argument. So each line is lexed
    whole with :mod:`shlex` (which understands quotes and leaves ``|`` / ``&&``
    as ordinary tokens) and the token after a ``-c`` is the body. A line that
    will not lex -- an unbalanced quote elsewhere in it -- is skipped, the same
    forgiving stance the CLI scan takes.
    """
    snippets: list[str] = []
    for expanded in _substitutions(script.replace("\\\n", " ")):
        for line in expanded.split("\n"):
            try:
                tokens = shlex.split(line)
            except ValueError:  # unbalanced quotes: not something we can read
                continue
            for index, token in enumerate(tokens):
                if token not in _PYTHON:
                    continue
                rest = tokens[index + 1 :]
                for offset, argument in enumerate(rest):
                    if argument == "-c" and offset + 1 < len(rest):
                        snippets.append(rest[offset + 1])
                        break
                    if not argument.startswith("-"):
                        break  # a module or script path, not `python -c CODE`
    return snippets


def _python_invocations():
    """``(workflow, step, body)`` for every ``python -c`` in every workflow."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text())
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                for body in _python_snippets(step.get("run") or ""):
                    yield path.name, step.get("name", "<unnamed>"), body


PYTHON_SNIPPETS = list(_python_invocations())


def _import_umbra(name: str, errors: list[str]):
    """Import an ``umbra_py`` module, telling drift apart from an absent extra.

    A renamed or removed module is drift and belongs in ``errors``. A module
    that exists but cannot import because an *optional* dependency is missing is
    a fact about this environment (the core ``[dev]`` test job installs no
    extras), not drift, so it is passed over. The two are distinguished by which
    module :class:`ModuleNotFoundError` reports absent: the ``umbra_py`` one that
    was named, or some third-party package underneath it.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        missing = error.name or ""
        if missing == name or name.startswith(f"{missing}.") or missing.startswith("umbra_py"):
            errors.append(f"cannot import {name!r}: {error}")
        return None  # an optional dependency is absent here; not drift
    except ImportError as error:  # pragma: no cover - a real umbra_py import bug
        errors.append(f"cannot import {name!r}: {error}")
        return None


def _umbra_aliases(tree: ast.Module, errors: list[str]) -> dict[str, object]:
    """Bind each local name in ``tree`` to the ``umbra_py`` object it imports.

    Resolves ``import umbra_py.x as p`` / ``import umbra_py.x`` / ``from
    umbra_py.x import y`` against the installed package, recording an unresolved
    import or a missing ``from`` name in ``errors`` as it goes.
    """
    aliases: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "umbra_py" and not alias.name.startswith("umbra_py."):
                    continue
                module = _import_umbra(alias.name, errors)
                if module is None:
                    continue
                if alias.asname:
                    aliases[alias.asname] = module
                else:
                    # `import umbra_py.pmtiles` binds the top package name; the
                    # submodule is reached as an attribute of it.
                    top = alias.name.split(".")[0]
                    top_module = _import_umbra(top, errors)
                    if top_module is not None:
                        aliases[top] = top_module
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("umbra_py"):
            module = _import_umbra(node.module or "", errors)
            if module is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not hasattr(module, alias.name):
                    errors.append(f"{node.module} has no attribute {alias.name!r}")
                else:
                    aliases[alias.asname or alias.name] = getattr(module, alias.name)
    return aliases


def _umbra_name_errors(body: str) -> list[str]:
    """Every way a ``python -c`` body drifts from the ``umbra_py`` it names.

    A body that will not compile is drift; so is an import that no longer
    resolves and an attribute read off an imported ``umbra_py`` module that no
    longer exists (``p.save_viewer`` after ``save_viewer`` is renamed). Reads
    off names that are not ``umbra_py`` imports are ignored -- the check is about
    this package's own surface, not about ``json`` or ``sys``.
    """
    try:
        tree = ast.parse(body)
    except SyntaxError as error:
        return [f"does not compile: {error}"]

    errors: list[str] = []
    aliases = _umbra_aliases(tree, errors)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        base = node.value
        # Only anchor on `<alias>.<attr>`; deeper chains are checked link by
        # link because every intermediate Attribute node is walked too.
        if isinstance(base, ast.Name) and base.id in aliases:
            target = aliases[base.id]
            if target is not None and not hasattr(target, node.attr):
                errors.append(f"{base.id}.{node.attr} does not resolve in {target!r}")
    return errors


@pytest.mark.parametrize(
    ("workflow", "step", "body"),
    PYTHON_SNIPPETS,
    ids=[f"{w}:{s}" for w, s, _ in PYTHON_SNIPPETS],
)
def test_python_snippet_resolves(workflow, step, body):
    """Each ``python -c`` body compiles and names existing ``umbra_py`` symbols."""
    errors = _umbra_name_errors(body)
    if errors:
        pytest.fail(f"{workflow} step {step!r}: `python -c` -> {'; '.join(errors)}")


def test_the_scan_actually_found_the_python_snippets():
    """A python-snippet scanner that matched nothing would pass forever.

    Pin the one that references library symbols by name -- the tiling step's
    ``save_viewer`` call is the reason this half of the suite exists.
    """
    found = "\n".join(body for _, _, body in PYTHON_SNIPPETS)
    assert "save_viewer" in found, found
    assert "CATALOG_INDEX_PMTILES_URL" in found, found


def test_a_renamed_library_symbol_would_be_caught():
    """The drift the python-snippet scan exists for, asserted rather than trusted.

    ``save_viewer`` renamed away is the exact failure that would kill the weekly
    publish while the Click parse stayed green, so a body that calls a
    non-existent one must be reported.
    """
    body = "import umbra_py.pmtiles as p; p.save_viewer_renamed_away()"
    assert _umbra_name_errors(body), "a missing umbra_py attribute must be flagged"
    # And the live symbol it stands in for still resolves, so the check is not
    # merely rejecting everything.
    assert not _umbra_name_errors("import umbra_py.pmtiles as p; p.save_viewer")
