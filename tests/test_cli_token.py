"""Offline tests for the ``--token`` Canopy commercial-archive flag on the
render/analysis commands.

``umbra search`` has long taken a ``--token`` that points the same ``search()``
interface at Umbra's authenticated Canopy archive instead of the open bucket
(see ``tests/test_canopy.py``). These tests cover the *funnel completion*: the
render/analysis verbs (``map``, ``gallery``, ``change``, ``timescan``,
``swipe``, ``chips``) now take the same ``--token`` and thread it through
``_gather_items`` to that commercial backend, so a paying customer renders and
analyses the archive they pay for with the identical flags.

Everything is driven offline: the dispatch is unit-tested, the token→Canopy flow
is exercised against a ``responses``-mocked STAC API (no credentials, no
network), and each command is checked for the shared option, the guard against
combining it with a local index, and the ``$UMBRA_CANOPY_TOKEN`` fallback.
"""

from __future__ import annotations

import click
import pytest
import responses
from click.testing import CliRunner

from umbra_py import cli as cli_mod
from umbra_py.catalog import UmbraCatalog
from umbra_py.constants import CANOPY_ARCHIVE_URL, CANOPY_TOKEN_ENV

# The six render/analysis commands that gather a search and their minimal
# invocation reaching ``_gather_items`` (a valid --out plus a search area).
_RENDER_COMMANDS = [
    ("map", ["map", "--bbox", "0,0,1,1", "--out", "m.html"]),
    ("gallery", ["gallery", "--area", "foo", "--out", "g.html"]),
    ("change", ["change", "--area", "foo", "--out", "c.png"]),
    ("timescan", ["timescan", "--area", "foo", "--out", "t.png"]),
    ("swipe", ["swipe", "--area", "foo", "--out", "s.html"]),
    ("chips", ["chips", "--area", "foo", "--out", "chips_out"]),
]


def _runner():
    try:
        return CliRunner(mix_stderr=False)  # click < 8.2
    except TypeError:
        return CliRunner()  # click >= 8.2


def _feature(item_id: str, dt: str, bbox: tuple) -> dict:
    return {
        "type": "Feature",
        "id": item_id,
        "bbox": list(bbox),
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [bbox[0], bbox[1]],
                    [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]],
                    [bbox[0], bbox[3]],
                    [bbox[0], bbox[1]],
                ]
            ],
        },
        "properties": {
            "datetime": dt,
            "sar:product_type": "GEC",
            "umbra:task_id": "Test Site",
        },
        "assets": {
            "GEC": {
                "href": f"https://api.canopy.umbra.space/data/{item_id}_GEC.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            }
        },
    }


# -- dispatch -----------------------------------------------------------------


def test_search_source_token_selects_canopy_backend():
    source, is_index = cli_mod._search_source(local=False, db_path=None, token="secret")
    assert is_index is False
    assert isinstance(source, UmbraCatalog)
    assert source.token == "secret"


def test_search_source_no_token_is_open_bucket():
    source, is_index = cli_mod._search_source(local=False, db_path=None, token=None)
    assert is_index is False
    assert isinstance(source, UmbraCatalog)
    assert source.token is None


@responses.activate
def test_gather_items_token_routes_to_commercial_archive():
    """A ``token`` sends the gather to the Canopy STAC API with bearer auth."""
    responses.add(
        responses.POST,
        CANOPY_ARCHIVE_URL,
        json={
            "type": "FeatureCollection",
            "features": [
                _feature("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
                _feature("b", "2024-02-10T12:00:00Z", (2, 2, 3, 3)),
            ],
            "links": [],
        },
        status=200,
    )
    items = cli_mod._gather_items(token="secret", start="2024-01-01", end="2024-12-31")
    assert [i.id for i in items] == ["a", "b"]
    assert len(responses.calls) == 1
    assert responses.calls[0].request.headers["Authorization"] == "Bearer secret"


# -- CLI wiring ---------------------------------------------------------------


@pytest.mark.parametrize("name,argv", _RENDER_COMMANDS, ids=[c[0] for c in _RENDER_COMMANDS])
def test_command_threads_token_to_gather(name, argv, monkeypatch):
    """``--token`` reaches ``_gather_items`` for every render/analysis command."""
    captured: dict = {}

    def fake_gather(**kwargs):
        captured.update(kwargs)
        raise click.ClickException("stop after capture")

    monkeypatch.setattr(cli_mod, "_gather_items", fake_gather)
    _runner().invoke(cli_mod.cli, [*argv, "--token", "secret"])
    assert captured.get("token") == "secret"


@pytest.mark.parametrize("name,argv", _RENDER_COMMANDS, ids=[c[0] for c in _RENDER_COMMANDS])
def test_command_token_env_var_fallback(name, argv, monkeypatch):
    """With no ``--token``, the command falls back to ``$UMBRA_CANOPY_TOKEN``."""
    captured: dict = {}

    def fake_gather(**kwargs):
        captured.update(kwargs)
        raise click.ClickException("stop after capture")

    monkeypatch.setattr(cli_mod, "_gather_items", fake_gather)
    _runner().invoke(cli_mod.cli, argv, env={CANOPY_TOKEN_ENV: "envtok"})
    assert captured.get("token") == "envtok"


@pytest.mark.parametrize("name,argv", _RENDER_COMMANDS, ids=[c[0] for c in _RENDER_COMMANDS])
def test_command_token_rejects_local_index(name, argv):
    """``--token`` (live commercial archive) cannot be combined with ``--local``."""
    result = _runner().invoke(cli_mod.cli, [*argv, "--token", "secret", "--local"])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output
