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
    assert len(SCHEMA_PATHS) >= 10


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


# --- Every example a schema carries validates against that schema --------------

# `examples` is a JSON Schema *annotation*: a validator never checks its members.
# So an example that drifts from the shape it illustrates -- an enum value
# renamed, a number turned string, a field a strict schema no longer allows --
# ships as valid-looking documentation that a consumer copying it would get
# wrong. These tests close that gap: every `examples` entry, at every depth and
# for a whole-document example alike, is validated against the subschema it sits
# on, resolving `$defs` and cross-file `$ref`s through the same registry the
# payload checks use.

_INSTANCE_KEYWORDS = frozenset({"examples", "default", "const", "enum"})


def _registry():
    return Registry().with_resources(
        [
            (schema["$id"], Resource(contents=schema, specification=DRAFT202012))
            for schema in (_load(path.name) for path in SCHEMA_PATHS)
        ]
    )


def _json_pointer(parts):
    if not parts:
        return "#"  # the whole document
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in parts]
    return "#/" + "/".join(escaped)


def _iter_schema_examples(node, path):
    """Yield ``(pointer, index, example)`` for every ``examples`` array in a schema.

    ``pointer`` locates the subschema carrying the array, so each example is
    checked against the shape it annotates rather than the whole document. The
    walk does not descend into instance-data keywords (``examples`` members,
    ``default``, ``const``, ``enum``), whose contents are values rather than
    subschemas and so could carry a nested ``examples`` key that is not one.
    """
    if isinstance(node, dict):
        members = node.get("examples")
        if isinstance(members, list):
            for index, example in enumerate(members):
                yield _json_pointer(path), index, example
        for key, value in node.items():
            if key in _INSTANCE_KEYWORDS:
                continue
            yield from _iter_schema_examples(value, path + [key])
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_schema_examples(value, path + [str(index)])


def _example_failures(schema, registry):
    """Every example in ``schema`` that does not validate against its subschema."""
    failures = []
    for pointer, index, example in _iter_schema_examples(schema, []):
        validator = jsonschema.Draft202012Validator(
            {"$ref": schema["$id"] + pointer}, registry=registry
        )
        for error in validator.iter_errors(example):
            failures.append(f"{pointer} examples[{index}]: {error.message}")
    return failures


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_every_example_validates_against_its_own_schema(path):
    schema = json.loads(path.read_text())
    failures = _example_failures(schema, _registry())
    assert not failures, f"{path.name}:\n" + "\n".join(failures)


def test_the_example_check_actually_found_examples():
    # A green run above must mean examples were validated, not that the traversal
    # found none -- the same vacuity guard the schema suite already applies to
    # itself. Both a corpus floor and the whole-document examples are pinned, so
    # dropping every example, or every root example, fails here.
    found = [
        pointer
        for path in SCHEMA_PATHS
        for pointer, _index, _example in _iter_schema_examples(_load(path.name), [])
    ]
    assert len(found) >= 100  # 126 today; a floor well below that
    assert found.count("#") >= 5  # the whole-document examples, checked at the root


def test_the_example_check_catches_a_drifted_example():
    # Proof the check has teeth: a real schema with an example that violates its
    # own subschema is caught. `scene-matches`'s `query` is a string.
    schema = _load("scene-matches.schema.json")
    schema["properties"]["query"]["examples"] = [12345]
    registry = Registry().with_resources(
        [(schema["$id"], Resource(contents=schema, specification=DRAFT202012))]
    )
    assert _example_failures(schema, registry), (
        "a numeric example for a string property must be caught"
    )


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


def test_stack_stats_speckle_detection_floor_validates(tmp_path):
    """The `detection` block, which a constant-valued series never produces.

    A block's looks are `mean**2 / variance`, so the flat fixture above has no
    variance to read and reports `looks: null` with no floor at all -- which is
    the nullable half of the contract. Speckled scenes exercise the other half.
    """
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from umbra_py import stack_stats, to_stack

    items = []
    for n in (1, 2):
        path = tmp_path / f"speckle{n}.tif"
        rng = np.random.default_rng(n)
        profile = {
            "driver": "GTiff",
            "height": 128,
            "width": 128,
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:32633",
            "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.sqrt(rng.gamma(1.0, 1.0, (128, 128))).astype("float32"), 1)
        items.append(_item(path, f"acq-{n}", f"2024-0{n}-08T12:00:00Z"))

    summary = stack_stats(to_stack(items, max_size=128, crs="utm"))
    assert summary["detection"]["looks"] > 0
    assert all(record["looks"] is not None for record in summary["passes"])
    _check("stack-stats.schema.json", summary)


def test_stack_stats_with_a_spatial_breakdown_validates(stack):
    from umbra_py import stack_stats, to_stack

    cube = to_stack(stack, max_size=32, crs="utm")
    _check("stack-stats.schema.json", stack_stats(cube, blocks=2))
    _check("stack-stats.schema.json", stack_stats(cube, blocks=2, block_series=True))


def test_stack_stats_spatial_per_block_detection_floor_validates(tmp_path):
    """The per-block `detection` (and `peak_block.stands_clear`), which the flat
    fixture never produces: a constant scene reads `looks: null` and so carries no
    floor at any level. A speckled series gives the blocks a floor to weigh."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from umbra_py import stack_stats, to_stack

    items = []
    for n in (1, 2):
        path = tmp_path / f"speckle{n}.tif"
        rng = np.random.default_rng(n)
        profile = {
            "driver": "GTiff",
            "height": 128,
            "width": 128,
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:32633",
            "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.sqrt(rng.gamma(1.0, 1.0, (128, 128))).astype("float32"), 1)
        items.append(_item(path, f"acq-{n}", f"2024-0{n}-08T12:00:00Z"))

    summary = stack_stats(to_stack(items, max_size=128, crs="utm"), blocks=2)
    block = summary["spatial"]["blocks"][0]
    assert set(block["detection"]) == {"false_alarm_fraction", "compared_cells", "stands_clear"}
    assert "stands_clear" in summary["spatial"]["peak_block"]
    _check("stack-stats.schema.json", summary)


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


# --- The chip dataset, its manifest records and its sidecar --------------------
#
# Three documents for one run, because a chip run has three consumers: an agent
# reading `umbra chips --json`, a training loader reading `manifest.jsonl` line
# by line, and whatever opens the directory later and finds `skipped.jsonl`. The
# summary `$ref`s the sidecar's schema for its own `skipped` entries rather than
# restating it, so the two cannot describe the same record differently.


def _emitted(payload: dict) -> dict:
    """``payload`` as a consumer receives it, rather than as Python holds it.

    ``ChipDataset.to_dict`` is printed, so the document under contract is the
    JSON one: the conversion's ``bbox`` is a tuple in the dataclass and an array
    on stdout, and it is the array a schema describes. Round-tripping also makes
    a value that could not be serialised at all fail here rather than in the
    command that prints it.
    """
    return json.loads(json.dumps(payload))


def _chip_run(tmp_path, **kwargs):
    """A real chipping run over a synthetic raster, returning the dataset."""
    from umbra_py.chips import write_chips

    from .test_chips import _item_for, _make_geotiff

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    return write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, **kwargs)


def test_chip_dataset_summary_validates(tmp_path):
    """The plain case: a published GEC chipped with nothing else asked for.

    None of the five conditional blocks is present, which is the half of the
    contract a converted run never exercises -- and the half a consumer is most
    likely to meet.
    """
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    payload = _emitted(_chip_run(tmp_path).to_dict())

    assert not {"conversion", "noise", "speckle", "skipped", "preflight"} & set(payload)
    _check("chip-dataset.schema.json", payload)


def test_every_chip_manifest_record_validates(tmp_path):
    """The manifest is the payload a training loader parses without printing it,
    so every line of a real one is checked rather than the summary that names it."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    dataset = _chip_run(tmp_path)
    lines = Path(dataset.manifest_path).read_text().strip().splitlines()

    assert len(lines) == dataset.chip_count == 4
    for line in lines:
        _check("chip-record.schema.json", json.loads(line))


def test_a_geojson_manifests_feature_properties_are_the_same_record(tmp_path):
    """`.geojson` is a different file, not a different record -- the contract is
    the record, so the feature's `properties` validate against it too."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    dataset = _chip_run(tmp_path, manifest="manifest.geojson")
    collection = json.loads(Path(dataset.manifest_path).read_text())

    assert collection["type"] == "FeatureCollection"
    for feature in collection["features"]:
        _check("chip-record.schema.json", feature["properties"])


def _converted_run(tmp_path, *, refuse=frozenset()):
    """A SICD run whose scenes carry a full set of conversion provenance tags."""
    from umbra_py.chips import SicdConversion, write_chips

    from .test_chips import _make_converted_cog, _refusing_preparer, _sicd_item

    cog = _make_converted_cog(
        tmp_path / "geocoded.tif",
        calibration="sigma0",
        noise_subtraction="estimated",
        noise_floor_db=-21.5,
        noise_floored_fraction=0.031,
        noise_floor_margin_db=4.25,
        speckle_filter="lee",
        speckle_window=5,
        speckle_enl_before=1.02,
        speckle_enl_after=8.4,
        speckle_looks=1.0,
        rtc_model="facet",
    )
    return write_chips(
        [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(
            calibration="sigma0",
            noise_subtract=True,
            noise_model="estimated",
            speckle_filter="lee",
            rtc=True,
            rtc_model="facet",
            bbox=(11.9, 36.0, 12.1, 36.2),
        ),
        preparer=_refusing_preparer(cog, refuse=refuse),
        skip_unsupported=True,
    )


def test_a_converted_chip_run_validates_with_every_conditional_block(tmp_path):
    """The other end: a complex run that converted, subtracted a floor, filtered
    speckle and could not include one of its passes -- so `conversion`, `noise`,
    `speckle`, `skipped`, `skipped_count` and `skipped_manifest` are all present.
    """
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    payload = _emitted(_converted_run(tmp_path, refuse={"acq-b"}).to_dict())

    assert payload["conversion"]["calibration"] == "sigma0"
    assert payload["noise"]["models"] == ["estimated"]
    assert payload["speckle"]["windows"] == [5]
    assert payload["skipped_count"] == 1
    assert payload["skipped_manifest"].endswith("skipped.jsonl")
    _check("chip-dataset.schema.json", payload)


def test_a_converted_chip_records_validate(tmp_path):
    """A chip cut from a complex product carries the conversion's own record,
    which is the half of the record schema an amplitude chip leaves null."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    dataset = _converted_run(tmp_path)
    record = _emitted(dataset.records[0].to_dict())

    assert record["calibration"] == "sigma0"
    assert record["noise_subtraction"] == "estimated"
    assert record["speckle_filter"] == "lee"
    assert record["rtc_model"] == "facet"
    for chip in dataset.records:
        _check("chip-record.schema.json", _emitted(chip.to_dict()))


def test_every_skipped_sidecar_line_validates(tmp_path):
    """The sidecar is what says a dataset has a hole rather than having been
    offered less, so it is read off disk rather than from the run that wrote it."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    dataset = _converted_run(tmp_path, refuse={"acq-b"})
    lines = Path(dataset.skipped_path).read_text().strip().splitlines()

    assert len(lines) == 1
    for line in lines:
        row = json.loads(line)
        assert row["stage"] == "conversion"
        _check("chip-skipped.schema.json", row)
        # And the same record inside the summary, which `$ref`s this schema.
        _check("chip-skipped.schema.json", _emitted(dataset.to_dict())["skipped"][0])


def test_the_skipped_footprint_validates_present_and_absent_but_not_malformed():
    """`bbox` locates the hole in space, and its contract is the ChipRecord
    bbox's: four numbers or null, and the strictness rejects anything else."""
    from umbra_py.chips import SkippedAcquisition

    located = SkippedAcquisition(
        item_id="acq-b", reason="metadata cannot support --calibrate", bbox=(12.0, 36.0, 12.2, 36.2)
    )
    _check("chip-skipped.schema.json", located.to_dict())

    footprintless = SkippedAcquisition(item_id="acq-c", reason="no readable product")
    assert footprintless.to_dict()["bbox"] is None
    _check("chip-skipped.schema.json", footprintless.to_dict())

    # A three-corner bbox is the drift a key-set comparison cannot see.
    drifted = located.to_dict()
    drifted["bbox"] = [12.0, 36.0, 12.2]
    assert list(_validator("chip-skipped.schema.json").iter_errors(drifted))


def test_a_preflighted_chip_run_validates(tmp_path):
    """The `preflight` block, and the `"preflight"` stage its drops are recorded
    under -- the one field that distinguishes a hole found before the download."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import SicdConversion, write_chips

    from .test_chips import (
        _make_converted_cog,
        _nitf_item,
        _recording_preparer,
        _write_products,
    )

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    dataset = write_chips(
        [_nitf_item(name, path) for name, path in products.items()],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, []),
        preflight=True,
    )
    payload = _emitted(dataset.to_dict())

    assert payload["preflight"]["checked"] == 3
    assert payload["preflight"]["skipped"] == 1
    assert payload["skipped"][0]["stage"] == "preflight"
    _check("chip-dataset.schema.json", payload)
    _check("chip-skipped.schema.json", payload["skipped"][0])


def test_the_chip_schemas_reject_a_key_and_a_type_that_drifted(tmp_path):
    """The strictness is the check, on the two documents most likely to grow a
    field: a new roll-up key on the summary, a new column on the record."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    dataset = _chip_run(tmp_path)

    payload = _emitted(dataset.to_dict())
    payload["chips_per_scene"] = 4  # a plausible-looking field that is not in the contract
    assert list(_validator("chip-dataset.schema.json").iter_errors(payload))

    record = _emitted(dataset.records[0].to_dict())
    record["valid_fraction"] = "1.0"  # the drift a key-set comparison cannot see
    assert list(_validator("chip-record.schema.json").iter_errors(record))


# --- The agent-facing surfaces (`umbra info` / `describe` / `ask` / `watch`) ---
#
# Each of these is read by a model or a scheduler rather than by a person, which
# is what makes the shape a contract rather than a formatting choice. The
# acquisition context card is the one they share: `umbra info --json` emits it
# alone, and a watch delta carries one per new acquisition, so `watch-delta`
# `$ref`s it rather than restating it.


def test_item_context_card_validates(sample_item_dict):
    from umbra_py.models import UmbraItem

    item = UmbraItem.from_dict(sample_item_dict, href="https://example.com/item.json")
    _check("item-context.schema.json", item.to_llm_context())


def test_item_context_card_validates_from_the_cli(monkeypatch, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    result = CliRunner().invoke(cli_mod.cli, ["info", "https://example.com/item.json", "--json"])

    assert result.exit_code == 0, result.output
    _check("item-context.schema.json", json.loads(result.stdout))


def test_item_context_card_validates_with_nothing_populated():
    # An item built from a bare dict: no geometry, no properties, no assets, no
    # href -- every nullable field of the card at once, which a catalog item
    # never exercises.
    from umbra_py.models import UmbraItem

    card = UmbraItem.from_dict({"id": "bare"}).to_llm_context()

    assert card["bbox"] is None and card["products"] == []
    _check("item-context.schema.json", card)


def test_scene_description_validates(monkeypatch, sample_item_dict):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    from .test_describe import PNG, describe_mod

    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: sample_item_dict)
    reply = json.dumps(
        {
            "summary": "A bright industrial site surrounded by dark fields.",
            "observed_features": ["bright rectangular structures"],
            "confidence": "medium",
            "caveats": ["dark fields could be low-backscatter crops or bare soil"],
        }
    )
    monkeypatch.setattr(describe_mod, "default_describer", lambda **k: lambda _m: reply)
    monkeypatch.setattr(describe_mod, "render_quicklook_png", lambda *a, **k: PNG)

    result = CliRunner().invoke(
        cli_mod.cli, ["describe", "https://example.com/item.json", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["image"]["source"] == "rendered"
    _check("scene-description.schema.json", payload)


def test_scene_description_validates_when_the_model_hedged_nothing():
    # `confidence` null and both lists empty -- the reply a terse model gives,
    # and the half of the schema the CLI fixture above never reaches.
    from umbra_py.describe import parse_description

    description = parse_description({"summary": "A quiet estuary."})

    assert description.confidence is None and description.image is None
    _check("scene-description.schema.json", description.to_dict())


def test_search_plan_validates(monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import planner as planner_mod

    reply = json.dumps(
        {
            "area": "Centerfield, Utah",
            "fuzzy": True,
            "start": "2024-03-01",
            "end": "2024-05-31",
            "product_types": ["GEC"],
            "polarizations": ["VV"],
            "min_incidence": 20,
            "max_incidence": 45,
            "max_resolution": 0.5,
            "limit": 3,
            "rationale": "named site over spring",
        }
    )
    monkeypatch.setattr(planner_mod, "default_planner", lambda **k: lambda _m: reply)

    result = CliRunner().invoke(cli_mod.cli, ["ask", "what changed at centerfield?", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"].startswith("umbra search")
    _check("search-plan.schema.json", payload)


def test_search_plan_validates_with_a_selected_area_of_interest(tmp_path, monkeypatch):
    # The `aoi` block: the geometry half of the determinism boundary, where the
    # model chose a caller-supplied polygon by name rather than authoring one.
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import planner as planner_mod

    aoi = tmp_path / "delta.geojson"
    aoi.write_text(
        json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
            }
        )
    )
    monkeypatch.setattr(
        planner_mod,
        "default_planner",
        lambda **k: lambda _m: json.dumps({"aoi": "delta", "rationale": "the named area"}),
    )

    result = CliRunner().invoke(
        cli_mod.cli, ["ask", "scenes over the delta", "--aoi", f"delta={aoi}", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["aoi"]["name"] == "delta" and payload["aoi"]["bbox"] == [0.0, 0.0, 1.0, 1.0]
    _check("search-plan.schema.json", payload)


def test_watch_delta_validates(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    from .test_watch import _seed_index

    db = tmp_path / "catalog.db"
    _seed_index(db)
    args = [
        "watch",
        "--local",
        "--index-db",
        str(db),
        "--state-db",
        str(db),
        "--area",
        "SiteA",
        "--json",
    ]

    first = CliRunner().invoke(cli_mod.cli, args)
    assert first.exit_code == 0, first.output
    payload = json.loads(first.stdout)
    assert payload["first_run"] is True and payload["new_count"] == 2
    _check("watch-delta.schema.json", payload)
    # Each new acquisition is a full context card, so the `$ref` has to hold.
    for card in payload["new_items"]:
        _check("item-context.schema.json", card)

    # And the quiet case a scheduler sees on every run after the first.
    second = json.loads(CliRunner().invoke(cli_mod.cli, args).stdout)
    assert second["new_items"] == [] and second["first_run"] is False
    _check("watch-delta.schema.json", second)


# --- The ranked-match lists (`umbra semantic` / `umbra embed`) ------------------


def test_task_matches_validate(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import semantic as semantic_mod

    from .test_semantic import _TASKS, _catalog_with_tasks, concept_embedder

    monkeypatch.setattr(semantic_mod, "default_embedder", lambda *, model=None: concept_embedder)
    catalog = _catalog_with_tasks(tmp_path, _TASKS)
    sem_db = tmp_path / "sem.db"
    runner = CliRunner()
    built = runner.invoke(
        cli_mod.cli, ["semantic", "build", "--db", str(catalog), "--semantic-db", str(sem_db)]
    )
    assert built.exit_code == 0, built.output

    result = runner.invoke(
        cli_mod.cli,
        ["semantic", "search", "grain storage nd", "--semantic-db", str(sem_db), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["matches"]
    _check("task-matches.schema.json", payload)


def test_scene_matches_validate(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import embed as embed_mod

    from .test_embed import _build, _item, image_embedder, scene_renderer, text_embedder

    monkeypatch.setattr(embed_mod, "default_image_embedder", lambda **k: image_embedder)
    monkeypatch.setattr(embed_mod, "default_text_embedder", lambda **k: text_embedder)
    monkeypatch.setattr(embed_mod, "_render_quicklook_asset", lambda it, **k: scene_renderer(it))

    edb = tmp_path / "e.db"
    _build(edb)

    # A query acquisition ...
    query = _item("flood", 9)
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda _url: query.raw)
    similar = CliRunner().invoke(
        cli_mod.cli, ["embed", "similar", query.href, "--embed-db", str(edb), "--json"]
    )
    assert similar.exit_code == 0, similar.output
    payload = json.loads(similar.stdout)
    assert payload["query"] == "flood-9" and payload["matches"]
    _check("scene-matches.schema.json", payload)

    # ... and a text query, which differs only in what `query` holds.
    searched = CliRunner().invoke(
        cli_mod.cli, ["embed", "search", "a flooded field", "--embed-db", str(edb), "--json"]
    )
    assert searched.exit_code == 0, searched.output
    _check("scene-matches.schema.json", json.loads(searched.stdout))


# --- The repeat-imaged-site coverage record (`umbra sites` / find_repeat_sites) --


def _site_pass(task, day, *, place=None, bbox=None, pols=None, assets=None):
    from umbra_py.models import UmbraItem

    props = {"datetime": f"2024-0{day}-08T00:00:00Z"}
    if pols is not None:
        props["sar:polarizations"] = pols
    item = UmbraItem.from_dict(
        {
            "type": "Feature",
            "id": f"{task}-{day}",
            "bbox": list(bbox) if bbox else [-112.0, 39.1, -111.9, 39.2],
            "geometry": None,
            "properties": props,
            "assets": assets or {},
        },
        href=f"https://x.s3.amazonaws.com/sar-data/tasks/{task}/t/{day}/i.json",
    )
    item.place = place
    return item


def test_site_coverage_record_validates():
    """A well-covered site: every field populated, which `umbra sites --json`
    and the `find_repeat_sites` agent tool both emit per record."""
    from umbra_py.coverage import rank_site_coverage

    passes = [
        _site_pass("Centerfield", d, place="Centerfield, Utah", pols=["VV"], assets={"GEC": {}})
        for d in (1, 3, 6)
    ]
    ranked = rank_site_coverage(passes, top=5, min_passes=2)

    assert ranked and ranked[0].span_days is not None
    for site in ranked:
        _check("site-coverage.schema.json", site.to_dict())


def test_site_coverage_record_validates_with_the_nullable_cadence_fields():
    """A single-dated-pass site: `span_days`, both revisit gaps and the whole
    date span are null with no second pass to measure against -- the nullable
    half of the contract, and the footprint is null when no pass carries one."""
    from umbra_py.coverage import site_coverage

    site = site_coverage("Lone", [_site_pass("Lone", 2, bbox=None)])
    payload = site.to_dict()
    # `from_dict` with an explicit bbox above always sets one, so force the
    # footprint-less case the schema's null branch describes.
    payload["bbox"] = None

    assert payload["span_days"] is None
    assert payload["comparable_span_days"] is None
    assert payload["min_revisit_days"] is None and payload["median_revisit_days"] is None
    assert payload["max_revisit_days"] is None
    assert payload["comparable_min_revisit_days"] is None
    assert payload["comparable_median_revisit_days"] is None
    assert payload["comparable_max_revisit_days"] is None
    _check("site-coverage.schema.json", payload)


def test_site_coverage_record_validates_with_comparable_below_the_raw_count():
    """A mixed-polarization site: `comparable_passes` (the largest same-pol dated
    subset) sits below `passes`, the non-trivial half of the field's contract."""
    from umbra_py.coverage import site_coverage

    passes = [_site_pass("Mixed", d, pols=["VV"]) for d in (1, 3, 6)]
    passes.append(_site_pass("Mixed", 8, pols=["HH"]))
    payload = site_coverage("Mixed", passes).to_dict()

    assert payload["passes"] == 4 and payload["comparable_passes"] == 3
    # comparable_hrefs is the usable subset: the three VV URLs, not the HH one.
    assert len(payload["comparable_hrefs"]) == 3
    assert set(payload["comparable_hrefs"]) < set(payload["hrefs"])
    # comparable_polarizations names that usable series: VV, a strict subset of the
    # site's [HH, VV] -- the array-not-null half of the field's contract.
    assert payload["polarizations"] == ["HH", "VV"]
    assert payload["comparable_polarizations"] == ["VV"]
    # The VV subset spans months 1->6 (Jan 8 -> Jun 8, 152d); the HH month-8 pass
    # stretches the whole range to Aug 8 (213d), so comparable_span_days sits below
    # span_days -- the temporal twin of comparable_passes below passes.
    assert payload["span_days"] == 213 and payload["comparable_span_days"] == 152
    _check("site-coverage.schema.json", payload)


def test_site_coverage_validates_from_the_cli(monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    passes = [_site_pass("Centerfield", d, pols=["VV"], assets={"GEC": {}}) for d in (1, 3, 6)]
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: passes)

    result = CliRunner().invoke(cli_mod.cli, ["sites", "--json"])

    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines
    for line in lines:
        _check("site-coverage.schema.json", json.loads(line))


def test_the_site_coverage_schema_rejects_drift():
    """Strictness: a plausible-looking extra field, and a bbox that lost a corner."""
    from umbra_py.coverage import site_coverage

    payload = site_coverage("Alpha", [_site_pass("Alpha", 1), _site_pass("Alpha", 3)]).to_dict()

    drifted = dict(payload)
    drifted["revisit_days"] = 2.0  # a field that is not in the contract
    assert list(_validator("site-coverage.schema.json").iter_errors(drifted))

    three_corner = dict(payload)
    three_corner["bbox"] = [-112.0, 39.1, -111.9]  # the drift a key-set check misses
    assert list(_validator("site-coverage.schema.json").iter_errors(three_corner))


def test_the_agent_surface_schemas_reject_drift(sample_item_dict):
    """Strictness on the two shapes an agent is most likely to read: a key added
    to a context card, and a confidence level that is not one of the three."""
    from umbra_py.describe import parse_description
    from umbra_py.models import UmbraItem

    card = UmbraItem.from_dict(sample_item_dict).to_llm_context()
    card["cloud_cover"] = 0  # a plausible-looking field SAR does not have
    assert list(_validator("item-context.schema.json").iter_errors(card))

    description = parse_description({"summary": "A quiet estuary."}).to_dict()
    description["confidence"] = "pretty sure"
    assert list(_validator("scene-description.schema.json").iter_errors(description))


# --- The async job document ----------------------------------------------------


def _job(**kwargs):
    from umbra_py import serve

    job = serve.RenderJob(
        id="0f9c",
        kind=kwargs.pop("kind", "stats"),
        cache_key="deadbeef",
        suffix="json",
        media_type="application/json",
        **kwargs,
    )
    return serve.job_to_dict(job, "http://testserver/")


def test_a_queued_render_job_validates():
    _check("render-job.schema.json", _job())


def test_a_succeeded_render_job_validates():
    from umbra_py import serve

    payload = _job(
        status=serve.JOB_SUCCEEDED, cached=True, started=None, finished="2026-08-03T00:00:00+00:00"
    )
    # The keys a finished job adds are exactly the two the contract makes
    # conditional: the result link and whether any work ran.
    assert payload["cache"] == "hit"
    assert [link["rel"] for link in payload["links"]] == ["self", "result"]
    _check("render-job.schema.json", payload)


def test_a_failed_render_job_validates():
    from umbra_py import serve

    payload = _job(status=serve.JOB_FAILED, error="Need the 'load' extra to stack.")
    assert "cache" not in payload  # a failed job never ran, so it hit nothing
    _check("render-job.schema.json", payload)


def test_the_job_schema_rejects_a_status_it_does_not_define():
    payload = _job()
    payload["status"] = "pending"  # a plausible fifth state that does not exist
    assert list(_validator("render-job.schema.json").iter_errors(payload))


# --- Reading the contracts from an installed umbra-py --------------------------


def test_the_accessor_finds_every_committed_schema():
    # `umbra_py.schemas` is how an installed package (and `umbra serve`'s
    # OpenAPI document) reaches these files; if it resolves a different set from
    # the one this suite validates, the contract shipped is not the one checked.
    from umbra_py import schemas

    assert set(schemas.schema_names()) == {
        path.name[: -len(schemas.SCHEMA_SUFFIX)] for path in SCHEMA_PATHS
    }
    assert schemas.schema_dir() == SCHEMA_DIR


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_the_accessor_loads_each_schema_by_either_name(path):
    from umbra_py import schemas

    stem = path.name[: -len(schemas.SCHEMA_SUFFIX)]
    committed = json.loads(path.read_text())
    assert schemas.load_schema(stem) == committed
    assert schemas.load_schema(path.name) == committed


def test_loading_a_schema_twice_hands_out_independent_objects():
    # `umbra serve` rewrites what it loads into OpenAPI components; a shared
    # cached dict would leak that rewrite into the next reader.
    from umbra_py import schemas

    first = schemas.load_schema("stack-stats")
    first["title"] = "clobbered"
    assert schemas.load_schema("stack-stats")["title"] != "clobbered"


def test_an_unknown_schema_name_names_the_published_ones():
    from umbra_py import schemas

    with pytest.raises(ValueError) as excinfo:
        schemas.load_schema("stack-statistics")
    assert "stack-statistics" in str(excinfo.value)
    assert "stack-stats" in str(excinfo.value)


def test_the_packaged_copy_wins_over_the_checkout(tmp_path, monkeypatch):
    """A wheel carries its own copy, and *that* is the one an install must read.

    The fallback exists for an editable install (no wheel is built, so nothing
    runs the `force-include`), which is what CI and the dev loop use -- so the
    packaged branch is the one no environment here exercises by accident.
    """
    from umbra_py import schemas

    packaged = tmp_path / schemas.PACKAGE_DATA_DIR
    packaged.mkdir()
    (packaged / "stack-stats.schema.json").write_text('{"title": "packaged"}')
    monkeypatch.setattr(schemas, "_candidate_dirs", lambda: (packaged, SCHEMA_DIR))

    assert schemas.schema_dir() == packaged
    assert schemas.load_schema("stack-stats")["title"] == "packaged"

    # ... and with no packaged copy present, the checkout answers instead.
    monkeypatch.setattr(schemas, "_candidate_dirs", lambda: (tmp_path / "absent", SCHEMA_DIR))
    assert schemas.schema_dir() == SCHEMA_DIR


def test_a_missing_install_says_where_it_looked(tmp_path, monkeypatch):
    from umbra_py import schemas

    monkeypatch.setattr(schemas, "_candidate_dirs", lambda: (tmp_path / "a", tmp_path / "b"))
    with pytest.raises(FileNotFoundError) as excinfo:
        schemas.schema_dir()
    assert str(tmp_path / "a") in str(excinfo.value)


def test_the_wheel_ships_the_schemas_where_the_accessor_looks():
    """The two ends of the package-data decision, checked against each other.

    `pyproject.toml` force-includes `docs/schemas` into the wheel and
    `umbra_py.schemas` reads it back by a hard-coded directory name. Nothing at
    runtime can notice them disagreeing -- an editable install never builds a
    wheel, so the fallback would quietly answer for a package that ships
    nothing. This is a parse rather than a build, for the same reason
    `tests/test_workflows.py` parses the workflows: it catches the drift that
    would actually happen (a renamed target) without a packaging round trip.
    """
    tomllib = pytest.importorskip("tomllib")  # stdlib from 3.11; skipped on 3.10

    from umbra_py import schemas

    pyproject = tomllib.loads((SCHEMA_DIR.parents[1] / "pyproject.toml").read_text())
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include == {"docs/schemas": f"umbra_py/{schemas.PACKAGE_DATA_DIR}"}
