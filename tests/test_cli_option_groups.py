"""Offline parity tests for the CLI's *shared* option groups.

`tests/test_geometry.py` locks the geography group (``--bbox`` / ``--place`` /
``--intersects``) across every gather command. This file does the same for the
other group that all of them share: the task-name filter, ``--area`` and its
``--fuzzy`` widener.

The reason it exists is the failure it prevents. ``--area`` was written out by
hand on thirteen commands, so ``umbra map`` -- the one nobody re-typed it on --
was the single gather command that could not name an Umbra site, and mapping one
site's coverage meant looking its bounding box up first. The same drift had
already cost the polygon filter thirteen commands and ``--place`` three. Writing
an option out per command is what lets a command miss it, so both groups are now
one shared definition each, and both are checked against one roster
(``conftest.GATHER_COMMANDS``): adding a gather command without the group fails
here rather than quietly shipping a front door with fewer filters than its
siblings.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from .conftest import GATHER_COMMANDS, command_argv


@pytest.mark.parametrize("spec", GATHER_COMMANDS, ids=lambda s: "-".join(command_argv(s)))
@pytest.mark.parametrize("option", ["--area", "--fuzzy"])
def test_every_gather_command_exposes_the_task_name_group(spec, option):
    """Naming the site is the cheapest filter the catalog has -- Umbra files every
    pass of a site under one task directory, so ``--area`` lists just that
    directory instead of scanning the archive. No command that gathers
    acquisitions should be missing it, or its typo-tolerant ``--fuzzy`` widener.
    """
    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(cli_mod.cli, [*command_argv(spec), "--help"])
    assert result.exit_code == 0, result.output
    assert option in result.output


# `watch` keeps its own state-diffing gather and the `index` commands walk the
# catalog through `CatalogIndex.build`/`update`, so neither routes through
# `_gather_items`; they are covered by the `--help` parity test above and by
# their own suites.
_FORWARDING_COMMANDS = [s for s in GATHER_COMMANDS if s[0] not in ("watch", "index")]


@pytest.mark.parametrize("spec", _FORWARDING_COMMANDS, ids=lambda s: "-".join(command_argv(s)))
def test_gather_commands_forward_area_and_fuzzy(spec, monkeypatch, tmp_path):
    """Exposing the option is half of it: each command must thread the name (and
    the fuzzy flag) down to the search backend, where ``area`` prunes the S3 walk
    to one task directory. The fake gather records the kwargs and returns nothing,
    so every command bails cleanly right after -- no render, no extra, no network.
    """
    from umbra_py import cli as cli_mod

    captured: dict = {}

    def _fake_gather(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("umbra_py.cli._shared._gather_items", _fake_gather)

    # `sites --local` ranks the whole index through `CatalogIndex.rank_sites`, not
    # `_gather_items`; its live path still forwards through the shared gather like
    # every sibling, so check it there (the local forwarding is pinned separately
    # in tests/test_coverage.py).
    local = [] if spec[0] == "sites" else ["--local"]
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(cli_mod.cli, [*spec, *local, "--area", "Centerfield", "--fuzzy"])

    assert captured, f"{spec[0]} did not call _gather_items"
    assert captured["area"] == "Centerfield"
    assert captured["fuzzy"] is True


def test_index_build_and_update_forward_fuzzy(monkeypatch, tmp_path):
    """The index walk takes the widened name too, so an index scoped to a site
    can be built from the same spelling the search commands accept."""
    from umbra_py import cli as cli_mod
    from umbra_py.index import CatalogIndex as _Index
    from umbra_py.index import UpdateResult

    captured: dict = {}

    def _fake_build(self, catalog=None, *, progress=None, **kwargs):
        captured.update(kwargs)
        return 0

    def _fake_update(self, catalog=None, *, progress=None, **kwargs):
        captured.update(kwargs)
        return UpdateResult(added=0, refreshed=0, scanned=0, start=None)

    monkeypatch.setattr(_Index, "build", _fake_build)
    monkeypatch.setattr(_Index, "update", _fake_update)

    db = tmp_path / "catalog.db"
    runner = CliRunner()
    for argv in (
        ["index", "build", "--db", str(db), "--area", "Centerfield", "--fuzzy"],
        ["index", "update", "--db", str(db), "--area", "Centerfield", "--fuzzy"],
    ):
        captured.clear()
        result = runner.invoke(cli_mod.cli, argv)
        assert result.exit_code == 0, result.output
        assert captured["area"] == "Centerfield"
        assert captured["fuzzy"] is True
