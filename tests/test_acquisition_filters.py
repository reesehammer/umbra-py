"""Offline tests for the SAR acquisition-property search filters.

The filters (``polarizations``, ``min_incidence`` / ``max_incidence``,
``max_resolution``) share a single predicate -- :meth:`UmbraItem.matches_filters`
-- so every search surface applies identical semantics. These tests exercise the
predicate directly plus each surface it is threaded through: the live open-bucket
walk, the local index, the read-through search, the Canopy archive, the ``umbra
search`` CLI and the MCP ``search_catalog`` tool. No network, no model, no extra.
"""

from __future__ import annotations

import json

import pytest
import responses
from click.testing import CliRunner

from umbra_py.catalog import UmbraCatalog
from umbra_py.cli import cli
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _item(
    item_id,
    *,
    task="SiteA",
    acq="2024-01-15-10-00-00_UMBRA-04",
    pols=("VV",),
    incidence=30.0,
    res_range=0.5,
    res_azimuth=0.5,
    include=("sar:polarizations", "view:incidence_angle", "sar:resolution"),
):
    """Build an UmbraItem carrying the SAR/view acquisition properties.

    ``include`` selects which property groups are present, so a test can model an
    item whose metadata is *missing* a field (to check the exclude-on-missing
    semantics). The href encodes task + acquisition so the index derives them.
    """
    base = f"{_BUCKET}/sar-data/tasks/{task}/{acq}/{acq}"
    props: dict = {"datetime": "2024-01-15T10:00:00Z", "sar:product_type": "GEC"}
    if "sar:polarizations" in include:
        props["sar:polarizations"] = list(pols)
    if "view:incidence_angle" in include and incidence is not None:
        props["view:incidence_angle"] = incidence
    if "sar:resolution" in include:
        if res_range is not None:
            props["sar:resolution_range"] = res_range
        if res_azimuth is not None:
            props["sar:resolution_azimuth"] = res_azimuth
    doc = {
        "id": item_id,
        "properties": props,
        "bbox": [0, 0, 1, 1],
        "geometry": None,
        "assets": {
            f"{acq}_GEC.tif": {
                "href": f"{base}_GEC.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            }
        },
    }
    return UmbraItem.from_dict(doc, href=f"{base}.stac.v2.json")


# --------------------------------------------------------------------------
# The predicate itself (UmbraItem.matches_filters)
# --------------------------------------------------------------------------


def test_no_arguments_matches_everything():
    assert _item("a").matches_filters() is True


def test_polarization_any_match_case_insensitive():
    it = _item("a", pols=("HH", "HV"))
    assert it.matches_filters(polarizations=["vv"]) is False
    assert it.matches_filters(polarizations=["hh"]) is True
    # Matches if the item exposes ANY requested polarization.
    assert it.matches_filters(polarizations=["VV", "HV"]) is True


def test_polarization_missing_metadata_is_excluded():
    it = _item("a", include=("view:incidence_angle", "sar:resolution"))
    assert it.matches_filters(polarizations=["VV"]) is False


def test_incidence_bounds_inclusive():
    it = _item("a", incidence=30.0)
    assert it.matches_filters(min_incidence=30.0) is True
    assert it.matches_filters(max_incidence=30.0) is True
    assert it.matches_filters(min_incidence=30.1) is False
    assert it.matches_filters(max_incidence=29.9) is False
    assert it.matches_filters(min_incidence=20.0, max_incidence=40.0) is True


def test_incidence_missing_metadata_is_excluded():
    it = _item("a", include=("sar:polarizations", "sar:resolution"))
    assert it.matches_filters(min_incidence=10.0) is False
    assert it.matches_filters(max_incidence=90.0) is False
    # But with no incidence bound set, a missing angle is no constraint.
    assert it.matches_filters(polarizations=["VV"]) is True


def test_max_resolution_requires_both_dimensions_fine():
    assert _item("a", res_range=0.5, res_azimuth=0.5).matches_filters(max_resolution=0.5) is True
    # Azimuth coarser than the threshold -> excluded.
    assert _item("a", res_range=0.5, res_azimuth=1.0).matches_filters(max_resolution=0.5) is False


def test_max_resolution_missing_value_is_excluded():
    it = _item("a", res_range=0.5, res_azimuth=None)
    assert it.matches_filters(max_resolution=1.0) is False


# --------------------------------------------------------------------------
# Local index (CatalogIndex.search) + agreement with the live catalog
# --------------------------------------------------------------------------

_VV_LOW = _item("vv-low", acq="2024-01-15-10-00-00_UMBRA-04", pols=("VV",), incidence=25.0)
_HH_HIGH = _item("hh-high", acq="2024-02-10-10-00-00_UMBRA-04", pols=("HH",), incidence=45.0)
_VV_COARSE = _item(
    "vv-coarse",
    acq="2024-03-10-10-00-00_UMBRA-04",
    pols=("VV",),
    incidence=35.0,
    res_range=1.0,
    res_azimuth=1.0,
)
_ALL = (_VV_LOW, _HH_HIGH, _VV_COARSE)


def _index(tmp_path, items=_ALL):
    idx = CatalogIndex(tmp_path / "catalog.db")
    for it in items:
        idx.add(it)
    idx.commit()
    return idx


def test_index_filters_by_polarization(tmp_path):
    idx = _index(tmp_path)
    got = {i.id for i in idx.search(polarizations=["VV"])}
    assert got == {"vv-low", "vv-coarse"}


def test_index_filters_by_incidence_range(tmp_path):
    idx = _index(tmp_path)
    got = {i.id for i in idx.search(min_incidence=30.0, max_incidence=40.0)}
    assert got == {"vv-coarse"}


def test_index_filters_by_max_resolution(tmp_path):
    idx = _index(tmp_path)
    got = {i.id for i in idx.search(max_resolution=0.5)}
    # Both fine-resolution scenes pass; the 1.0 m one is dropped.
    assert got == {"vv-low", "hh-high"}


def test_index_filters_combine(tmp_path):
    idx = _index(tmp_path)
    got = {i.id for i in idx.search(polarizations=["VV"], max_incidence=30.0)}
    assert got == {"vv-low"}


def test_index_and_live_paths_agree(tmp_path, monkeypatch):
    """The index and the live open-bucket walk must return the same ids for the
    same acquisition-property filter -- one predicate, applied identically."""
    idx = _index(tmp_path)
    index_ids = sorted(i.id for i in idx.search(polarizations=["VV"], max_incidence=30.0))

    # A fake live catalog whose per-task walk yields the same three items; the
    # search() loop is what applies matches_filters, so this proves the live
    # path and the index path share one predicate.
    cat = UmbraCatalog()
    monkeypatch.setattr(cat, "_list_prefix", lambda prefix: (["sar-data/tasks/SiteA/"], []))
    monkeypatch.setattr(cat, "_walk_task", lambda prefix, start, end: iter(_ALL))
    live_ids = sorted(i.id for i in cat.search(polarizations=["VV"], max_incidence=30.0))
    assert live_ids == index_ids == ["vv-low"]


# --------------------------------------------------------------------------
# Canopy commercial archive (client-side filter over the STAC API)
# --------------------------------------------------------------------------


@responses.activate
def test_archive_applies_acquisition_filters_client_side():
    features = [it.raw | {"id": it.id} for it in _ALL]
    responses.add(
        responses.POST,
        "https://api.canopy.umbra.space/archive/search",
        json={"type": "FeatureCollection", "features": features, "links": []},
        status=200,
    )
    cat = UmbraCatalog(token="secret")
    got = {i.id for i in cat.search(polarizations=["HH"])}
    assert got == {"hh-high"}


# --------------------------------------------------------------------------
# CLI (umbra search --local)
# --------------------------------------------------------------------------


def test_cli_search_local_filters_by_polarization(tmp_path):
    _index(tmp_path).close()
    db = str(tmp_path / "catalog.db")
    runner = CliRunner()
    res = runner.invoke(cli, ["search", "--local", "--db", db, "--pol", "HH", "--json"])
    assert res.exit_code == 0, res.output
    ids = {json.loads(line)["id"] for line in res.output.splitlines() if line.startswith("{")}
    assert ids == {"hh-high"}


def test_cli_search_local_filters_by_incidence_and_resolution(tmp_path):
    _index(tmp_path).close()
    db = str(tmp_path / "catalog.db")
    runner = CliRunner()
    res = runner.invoke(
        cli,
        [
            "search",
            "--local",
            "--db",
            db,
            "--max-incidence",
            "30",
            "--max-resolution",
            "0.5",
            "--json",
        ],
    )
    assert res.exit_code == 0, res.output
    ids = {json.loads(line)["id"] for line in res.output.splitlines() if line.startswith("{")}
    assert ids == {"vv-low"}


# --------------------------------------------------------------------------
# MCP search_catalog forwards the filters to the backend
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Render / analysis commands forward the filters to the search backend
# --------------------------------------------------------------------------


_FILTER_ARGS = [
    "--pol",
    "VV",
    "--min-incidence",
    "20",
    "--max-incidence",
    "40",
    "--max-resolution",
    "0.5",
]

_EXPECTED_FORWARDED = {
    "polarizations": ["VV"],
    "min_incidence": 20.0,
    "max_incidence": 40.0,
    "max_resolution": 0.5,
}


@pytest.mark.parametrize(
    "argv",
    [
        ["change", "--out", "c.png", "--area", "SiteA"],
        ["timescan", "--out", "t.png", "--area", "SiteA"],
        ["swipe", "--out", "s.html", "--area", "SiteA"],
        ["gallery", "--out", "g.html", "--area", "SiteA"],
        ["map", "--out", "m.html", "--bbox", "0,0,1,1"],
        ["chips", "--out", "chips_out", "--area", "SiteA"],
    ],
)
def test_render_commands_forward_acquisition_filters(argv, monkeypatch, tmp_path):
    """Each render/analysis command that gathers a search must thread the shared
    SAR acquisition filters down to the search backend -- the ``--pol VV`` change
    composite the docs promise. The fake ``_gather_items`` records the kwargs and
    returns no items, so every command exits early with a clean error *after* the
    kwargs are captured -- no render, no viz extra, no network."""
    from umbra_py import cli as cli_mod

    captured: dict = {}

    def _fake_gather(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli_mod, "_gather_items", _fake_gather)

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        # --local keeps the (patched) gather offline; the empty result makes the
        # command bail cleanly, which is fine -- we only assert what it searched.
        runner.invoke(cli, [argv[0], "--local", *argv[1:], *_FILTER_ARGS])

    assert captured, f"{argv[0]} did not call _gather_items"
    for key, val in _EXPECTED_FORWARDED.items():
        assert captured[key] == val, (argv[0], key, captured.get(key))


def test_render_command_unset_filters_forward_as_none(monkeypatch, tmp_path):
    """With no filter flags, the render command forwards ``None`` for each --
    an empty ``--pol`` tuple becomes ``None``, not ``[]`` -- so an unfiltered
    render searches exactly as before."""
    from umbra_py import cli as cli_mod

    captured: dict = {}

    def _fake_gather(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli_mod, "_gather_items", _fake_gather)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        runner.invoke(cli, ["change", "--local", "--out", "c.png", "--area", "SiteA"])

    assert captured["polarizations"] is None
    assert captured["min_incidence"] is None
    assert captured["max_incidence"] is None
    assert captured["max_resolution"] is None


def test_mcp_search_catalog_forwards_acquisition_filters(monkeypatch):
    mcp = pytest.importorskip("umbra_py.mcp_server")

    class _FakeCatalog:
        def search(self, **kwargs):
            _FakeCatalog.kwargs = kwargs
            return iter([])

    monkeypatch.setattr(mcp, "UmbraCatalog", lambda *a, **k: _FakeCatalog())
    mcp.search_catalog(
        area="anywhere",
        polarizations=["VV"],
        min_incidence=20.0,
        max_incidence=40.0,
        max_resolution=0.5,
        local=False,
    )
    kw = _FakeCatalog.kwargs
    assert kw["polarizations"] == ["VV"]
    assert kw["min_incidence"] == 20.0
    assert kw["max_incidence"] == 40.0
    assert kw["max_resolution"] == 0.5
