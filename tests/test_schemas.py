"""Offline tests that the published JSON Schemas describe what the code emits.

``docs/schemas/`` is public API: an agent or a script is invited to depend on
those shapes. Until now the suite checked two of them by comparing key *sets*,
which catches a renamed field and nothing else -- not a type that changed, not a
value that went null, not a key that appeared. So the contract was a document
rather than a check.

Everything here validates a payload produced by a real surface against the
committed schema with a real validator. The measurement documents
(``stack-stats``, ``stack-provenance``, ``preflight``) are the ones worth
pinning hardest, because each is emitted by three front doors -- the CLI's
``--json``, ``umbra serve``'s artifact routes and the MCP / LangChain /
LlamaIndex agent tools -- from one ``to_dict()``. That identity is already
pinned per surface (``tests/test_serve.py``, ``tests/test_mcp_server.py``), so
validating the one document here validates all three without rebuilding their
fixtures.

Every schema is strict (``additionalProperties: false``), which is the point: a
key added to a payload and not to its schema fails here rather than in a
consumer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from referencing import Registry, Resource  # noqa: E402
from referencing.jsonschema import DRAFT202012  # noqa: E402

from umbra_py.exceptions import CatalogError  # noqa: E402

from .test_preflight import build_nitf  # noqa: E402

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"
SCHEMA_PATHS = sorted(SCHEMA_DIR.glob("*.schema.json"))


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _validator(name: str):
    """A validator for ``name`` that resolves the suite's own cross-file refs."""
    schemas = [_load(path.name) for path in SCHEMA_PATHS]
    registry = Registry().with_resources(
        [
            (schema["$id"], Resource(contents=schema, specification=DRAFT202012))
            for schema in schemas
        ]
    )
    return jsonschema.Draft202012Validator(_load(name), registry=registry)


def _check(name: str, payload: dict) -> None:
    """Validate ``payload``, reporting every failure rather than only the first."""
    errors = sorted(_validator(name).iter_errors(payload), key=lambda e: list(e.absolute_path))
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


# --- The schema suite itself ---------------------------------------------------


def test_the_suite_is_not_empty():
    # A glob that matched nothing would make every test below vacuously green.
    assert len(SCHEMA_PATHS) >= 7


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_every_schema_is_a_valid_2020_12_schema(path):
    jsonschema.Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_every_schema_id_ends_in_its_own_filename(path):
    # The `$id` is what a cross-file `$ref` resolves against, so a stale one
    # silently points a reference at the wrong document.
    assert json.loads(path.read_text())["$id"].endswith(f"/{path.name}")


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_every_schema_is_listed_in_the_readme(path):
    # The README table is how a reader finds a schema at all; an unlisted one is
    # a contract nobody knows exists.
    assert path.name in (SCHEMA_DIR / "README.md").read_text()


def test_the_readme_lists_no_schema_that_is_gone():
    import re

    readme = (SCHEMA_DIR / "README.md").read_text()
    named = set(re.findall(r"[\w.-]+\.schema\.json", readme))
    assert named == {path.name for path in SCHEMA_PATHS}


# --- The error contract --------------------------------------------------------


def test_error_payload_validates():
    _check(
        "error.schema.json", CatalogError("could not read catalog", hint="check the URL").to_dict()
    )


def test_error_payload_validates_with_a_null_hint():
    _check("error.schema.json", CatalogError("boom").to_dict())


# --- The two other CLI success shapes ------------------------------------------


def test_download_manifest_validates(tmp_path, monkeypatch, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: sample_item_dict)

    def fake_download_item(item, dest, assets, overwrite, progress):
        (name,) = assets
        path = tmp_path / f"{item.id}_{name}.tif"
        path.write_bytes(b"sar-bytes" * 10)
        return [path]

    monkeypatch.setattr("umbra_py.cli.scenes.download_item", fake_download_item)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "download",
            "https://example.com/item.json",
            "--asset",
            "GEC",
            "--dest",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    _check("download.schema.json", json.loads(result.stdout))


def test_index_info_validates(tmp_path, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as index:
        index.add(UmbraItem.from_dict(sample_item_dict))
        index.set_meta("built_at", "2026-07-01")

    result = CliRunner().invoke(cli_mod.cli, ["index", "info", "--db", str(db), "--json"])

    assert result.exit_code == 0, result.output
    _check("index-info.schema.json", json.loads(result.stdout))


# --- Datacube statistics (`stack_stats`) ---------------------------------------


def _scene(path, value, *, tags=None, height=40, width=40):
    """A constant-valued north-up UTM GeoTIFF, optionally carrying conversion tags."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.full((height, width), float(value), dtype="float32"), 1)
        if tags:
            dst.update_tags(**tags)
    return path


def _item(tif, item_id, when, href=None):
    from umbra_py.models import UmbraItem

    item = UmbraItem(id=item_id, properties={"datetime": when}, href=href)
    item.asset_href = lambda asset="GEC", _p=str(tif): _p  # type: ignore[method-assign]
    return item


def _series(tmp_path, tags=None):
    """Three same-footprint passes at 2 / 4 / 8, oldest first."""
    return [
        _item(
            _scene(tmp_path / f"s{n}.tif", value, tags=tags),
            f"acq-{n}",
            f"2024-0{n}-08T12:00:00Z",
            f"https://example.com/{n}.json",
        )
        for n, value in ((1, 2.0), (2, 4.0), (3, 8.0))
    ]


@pytest.fixture
def stack(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    return _series(tmp_path)


def test_stack_stats_default_summary_validates(stack):
    from umbra_py import stack_stats, to_stack

    _check("stack-stats.schema.json", stack_stats(to_stack(stack, max_size=32, crs="utm")))


def test_stack_stats_with_a_spatial_breakdown_validates(stack):
    from umbra_py import stack_stats, to_stack

    cube = to_stack(stack, max_size=32, crs="utm")
    _check("stack-stats.schema.json", stack_stats(cube, blocks=2))
    _check("stack-stats.schema.json", stack_stats(cube, blocks=2, block_series=True))


def test_stack_stats_on_a_geographic_grid_validates(stack):
    # No projected CRS, so `cell_area_m2` and every `changed_area_km2` are null
    # -- the nullable half of the schema, which a projected cube never exercises.
    from umbra_py import stack_stats, to_stack

    summary = stack_stats(to_stack(stack, max_size=32), blocks=2)
    assert summary["grid"]["cell_area_m2"] is None
    assert summary["net_change"]["changed_area_km2"] is None
    _check("stack-stats.schema.json", summary)


def test_stack_stats_of_a_single_pass_validates(stack):
    # `net_change` is null and every pass's `change_vs_previous` is too: there is
    # no previous pass to compare against.
    from umbra_py import stack_stats, to_stack

    summary = stack_stats(to_stack(stack[:1], max_size=32, crs="utm"))
    assert summary["net_change"] is None
    _check("stack-stats.schema.json", summary)


def test_windowed_stack_stats_validates(stack):
    # The one mode that adds keys (`quantile_method` / `quantile_bin_db`), which
    # is exactly the drift a key-set comparison of one payload would miss.
    pytest.importorskip("dask")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(stack, max_size=32, crs="utm", lazy=True, chunk_size=16)
    summary = stack_stats(cube, windowed=True)
    assert summary["quantile_method"] == "histogram"
    _check("stack-stats.schema.json", summary)


def test_stack_stats_carries_the_sources_conversion_record(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    from umbra_py import stack_stats, to_stack

    tags = {
        "UMBRA_CALIBRATION": "gamma0",
        "UMBRA_RTC_MODEL": "facet",
        "UMBRA_SCALE": "amplitude",
        "UMBRA_UNITS": "amplitude",
        "UMBRA_NOISE_SUBTRACTION": "estimated",
        "UMBRA_SPECKLE_FILTER": "lee",
        "UMBRA_SPECKLE_WINDOW": "5",
    }
    summary = stack_stats(to_stack(_series(tmp_path, tags), max_size=32, crs="utm"))
    assert summary["provenance"]["calibration"] == "gamma0"
    _check("stack-stats.schema.json", summary)


def test_the_stats_schema_rejects_an_unknown_key(stack):
    """The strictness is the check: a key the schema does not know fails here."""
    from umbra_py import stack_stats, to_stack

    summary = stack_stats(to_stack(stack, max_size=32, crs="utm"))
    summary["mean_delta_db"] = 1.0  # a plausible-looking field that is not in the contract
    assert list(_validator("stack-stats.schema.json").iter_errors(summary))

    # And a type that drifted, which is the failure a key-set comparison cannot see.
    del summary["mean_delta_db"]
    summary["passes"][0]["valid_fraction"] = "1.0"
    assert list(_validator("stack-stats.schema.json").iter_errors(summary))


# --- The render manifest, whose `stats` key is the document above --------------


def test_stack_manifest_validates_including_its_inline_stats(tmp_path, monkeypatch):
    """`umbra stack --stats --json` emits a manifest carrying the stats summary.

    The manifest schema `$ref`s the stats schema for that key, so this validates
    the two contracts against each other on one real payload.
    """
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    items = _series(tmp_path)
    paths = {item.id: item.asset_href() for item in items}
    stac = {
        f"http://example.com/{item.id}.json": {
            "id": item.id,
            "properties": {"datetime": item.properties["datetime"]},
            "assets": {},
        }
        for item in items
    }
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href",
        lambda self, asset="GEC": paths[self.id],
    )

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "stack",
            *stac,
            "--out",
            str(tmp_path / "cube.tif"),
            "--max-size",
            "16",
            "--crs",
            "utm",
            "--stats",
            "--blocks",
            "2",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert "stats" in manifest
    _check("render-manifest.schema.json", manifest)


# --- Stack provenance ----------------------------------------------------------


def test_stack_provenance_of_an_agreeing_series_validates(stack):
    from umbra_py import stack_provenance

    report = stack_provenance(stack)
    assert report.agrees
    _check("stack-provenance.schema.json", report.to_dict())


def test_stack_provenance_of_a_mixed_selection_validates(tmp_path, stack):
    """The mixed case carries the two keys an agreeing one omits: `refusal`, and
    an `unreadable` entry for a source that could not be opened at all."""
    from umbra_py import stack_provenance

    converted = _item(
        _scene(
            tmp_path / "converted.tif",
            3.0,
            tags={"UMBRA_CALIBRATION": "gamma0", "UMBRA_SCALE": "amplitude"},
        ),
        "acq-4",
        "2024-04-08T12:00:00Z",
        "https://example.com/4.json",
    )
    absent = _item(tmp_path / "gone.tif", "acq-5", "2024-05-08T12:00:00Z")

    report = stack_provenance([*stack, converted, absent])
    payload = report.to_dict()

    assert not report.agrees
    assert len(payload["groups"]) == 2
    assert payload["unreadable"][0]["item_id"] == "acq-5"
    assert "refusal" in payload
    _check("stack-provenance.schema.json", payload)


# --- The preflight report ------------------------------------------------------

_SICD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<SICD xmlns="urn:SICD:1.2.1">
  <CollectionInfo><CoreName>2024-02-08-01-02-03_UMBRA-05</CoreName></CollectionInfo>
  <ImageData>
    <NumRows>4096</NumRows><NumCols>8192</NumCols>
    <SCPPixel><Row>2048</Row><Col>4096</Col></SCPPixel>
  </ImageData>
  <Grid><Row><SS>0.15</SS></Row><Col><SS>0.12</SS></Col></Grid>
  <ImageFormation><TxRcvPolarizationProc>V:V</TxRcvPolarizationProc></ImageFormation>
</SICD>
"""

_SICD_XML_CALIBRATED = _SICD_XML.replace(
    "</SICD>",
    """  <Radiometric>
    <SigmaZeroSFPoly order1="0" order2="0">
      <Coef exponent1="0" exponent2="0">2.5</Coef>
    </SigmaZeroSFPoly>
    <NoiseLevel>
      <NoiseLevelType>ABSOLUTE</NoiseLevelType>
      <NoisePoly order1="0" order2="0">
        <Coef exponent1="0" exponent2="0">-20.0</Coef>
      </NoisePoly>
    </NoiseLevel>
  </Radiometric>
</SICD>""",
)


def _sicd_item(tmp_path, name, xml, item_id):
    path = tmp_path / f"{name}.nitf"
    path.write_bytes(build_nitf(xml))
    item = _item(path, item_id, "2024-02-08T01:02:03Z", f"https://example.com/{item_id}.json")
    item.asset_href = lambda asset="SICD", _p=str(path): _p  # type: ignore[method-assign]
    return item


def test_preflight_report_validates(tmp_path):
    """One product that can answer, one that cannot, one that is not there.

    All three verdicts in one payload: a cleared read, a refusal carrying the
    product's own words and a hint, and a `"product"`-scope failure with a null
    `capabilities`.
    """
    pytest.importorskip("numpy")
    from umbra_py.preflight import preflight_items

    items = [
        _sicd_item(tmp_path, "calibrated", _SICD_XML_CALIBRATED, "acq-cal"),
        _sicd_item(tmp_path, "plain", _SICD_XML, "acq-plain"),
        _item(tmp_path / "absent.nitf", "acq-gone", "2024-03-08T00:00:00Z"),
    ]
    items[2].asset_href = lambda asset="SICD", _p=str(tmp_path / "absent.nitf"): _p  # type: ignore[method-assign]

    report = preflight_items(items, asset="SICD", calibration="sigma0", workers=1)
    payload = report.to_dict()

    assert payload["supported_count"] == 1
    assert payload["results"][1]["reason"]
    assert payload["missing_count"] == 1
    assert payload["results"][2]["capabilities"] is None
    _check("preflight.schema.json", payload)


def test_preflight_report_of_a_clean_selection_validates(tmp_path):
    from umbra_py.preflight import preflight_items

    report = preflight_items(
        [_sicd_item(tmp_path, "plain", _SICD_XML, "acq-plain")], asset="SICD", workers=1
    )
    payload = report.to_dict()

    assert payload["supported_count"] == 1
    assert payload["calibration"] is None
    _check("preflight.schema.json", payload)
