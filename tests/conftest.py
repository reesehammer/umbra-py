import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"

# Every subcommand that gathers acquisitions by search, with the minimum extra
# arguments each needs to reach its gather. Kept in one place so a new gather
# command is a one-line addition rather than a silently missing filter -- the
# shared-option parity suites (`test_geometry.py`, `test_cli_option_groups.py`)
# all parametrize over this list.
GATHER_COMMANDS = [
    ["change", "--out", "c.png"],
    ["timescan", "--out", "t.png"],
    ["swipe", "--out", "s.html"],
    ["stack", "--stats"],
    ["gallery", "--out", "g.html"],
    ["map", "--out", "m.geojson"],
    ["demo", "--out", "d.html"],
    ["tiles", "--out", "t.pmtiles"],
    ["chips", "--out", "chips_out"],
    ["showcase", "--out", "site"],
    ["watch"],
    ["index", "build"],
    ["index", "update"],
    ["embed", "build"],
]


def command_argv(spec):
    """The subcommand path of a `GATHER_COMMANDS` entry (e.g. ``['index', 'build']``)."""
    return [a for a in spec if not a.startswith("-")][: 2 if spec[0] in ("index", "embed") else 1]


@pytest.fixture
def sample_item_dict() -> dict:
    return json.loads((DATA_DIR / "sample_item.json").read_text())
