"""Offline tests for ML chip preparation (``umbra_py.chips``).

Like ``test_load.py`` these build a tiny real GeoTIFF on disk and point a
synthetic ``UmbraItem`` at it, so the whole tile-read + manifest path runs end
to end with no network access and no model call.
"""

from __future__ import annotations

import dataclasses
import json
import math
from pathlib import Path

import pytest

from umbra_py.models import UmbraItem


def _make_geotiff(path, *, width=20, height=20, nodata_corner=True):
    """Write a small north-up UTM GeoTIFF and return (path, bounds, crs)."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    data = (np.arange(width * height, dtype="float32") + 1.0).reshape(height, width)
    if nodata_corner:
        # A block of non-positive pixels in the top-left, so a corner chip is
        # partly invalid and min_valid can filter it.
        data[0:5, 0:5] = 0.0

    transform = from_origin(500000.0, 4000000.0, 10.0, 10.0)  # 10 m pixels
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        bounds = dst.bounds
        crs = dst.crs
    return path, bounds, crs


def _item_for(tif_path, **props):
    properties = {
        "datetime": "2024-02-08T12:00:00Z",
        "platform": "Umbra-08",
        "sar:polarizations": ["VV"],
        "sar:product_type": "GEC",
        "view:incidence_angle": 42.0,
        "sar:resolution_range": 0.5,
        "sar:resolution_azimuth": 0.5,
    }
    properties.update(props)
    item = UmbraItem(id="test-acq", properties=properties)
    item.asset_href = lambda asset="GEC": str(tif_path)  # type: ignore[method-assign]
    return item


def test_chip_item_grid_shape_and_count(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, crs = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)

    # 20x20 raster / 10 px non-overlapping -> a 2x2 grid.
    assert len(records) == 4
    assert {(r.row, r.col) for r in records} == {(0, 0), (0, 1), (1, 0), (1, 1)}
    for rec in records:
        chip = tmp_path / "chips" / rec.path
        assert chip.exists()
        with rasterio.open(chip) as ds:
            assert (ds.width, ds.height) == (10, 10)
            assert ds.crs == crs
            assert ds.tags()["item_id"] == "test-acq"
            assert ds.tags()["attribution"].startswith("Contains Umbra")


def test_partial_edge_tiles_are_dropped(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    # 25x25 with chip_size 10 -> only the 2x2 full-tile grid fits; the 5 px
    # right/bottom strips are dropped (fixed-size promise).
    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=25, height=25, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)
    assert len(records) == 4
    assert max(r.col for r in records) == 1
    assert max(r.row for r in records) == 1


def test_stride_overlaps_tiles(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    # stride 5, chip 10 over 20 px -> origins 0,5,10 in each axis -> 3x3 = 9.
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, stride=5)
    assert len(records) == 9


def test_min_valid_filters_nodata_corner(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=True)

    # The top-left 10x10 chip is 25% zeros (5x5 of 100) -> valid_fraction 0.75.
    keep_all = chip_item(_item_for(tif), tmp_path / "a", chip_size=10, min_valid=0.0)
    assert len(keep_all) == 4
    corner = next(r for r in keep_all if (r.row, r.col) == (0, 0))
    assert corner.valid_fraction == pytest.approx(0.75)

    # Requiring >90% valid drops the corner chip but keeps the other three.
    filtered = chip_item(_item_for(tif), tmp_path / "b", chip_size=10, min_valid=0.9)
    assert len(filtered) == 3
    assert (0, 0) not in {(r.row, r.col) for r in filtered}


def test_record_carries_geo_and_acquisition_metadata(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, bounds, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)
    rec = next(r for r in records if (r.row, r.col) == (0, 0))

    assert rec.item_id == "test-acq"
    assert rec.asset == "GEC"
    assert rec.window == [0, 0, 10, 10]
    assert rec.units == "amplitude"
    assert rec.datetime == "2024-02-08T12:00:00+00:00"
    assert rec.platform == "Umbra-08"
    assert rec.polarizations == ["VV"]
    assert rec.incidence_angle_deg == 42.0
    assert rec.resolution_range_m == 0.5
    assert rec.license == "CC-BY-4.0"
    assert len(rec.transform) == 6
    assert len(rec.bbox) == 4
    # Geographic bbox is lon/lat (EPSG:32633 zone 33N -> ~12-15 E, ~36 N).
    min_lon, min_lat, max_lon, max_lat = rec.bbox
    assert min_lon < max_lon and min_lat < max_lat
    assert -180 <= min_lon <= 180 and -90 <= min_lat <= 90


def test_db_scale_writes_decibels(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, db=True)
    assert all(r.units == "dB" for r in records)

    rec = next(r for r in records if (r.row, r.col) == (0, 0))
    with rasterio.open(tmp_path / "chips" / rec.path) as ds:
        data = ds.read([1])[0]
    assert np.isfinite(data).all()
    # The top-left pixel is amplitude 1.0 (arange + 1) -> 20*log10(1) == 0 dB.
    assert data[0, 0] == pytest.approx(0.0)
    # A pixel of known amplitude maps to 20*log10(amp).
    assert data[0, 5] == pytest.approx(20.0 * math.log10(6.0))


def test_npy_format(tmp_path):
    pytest.importorskip("numpy")
    np = pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, fmt="npy")

    assert all(r.path.endswith(".npy") for r in records)
    arr = np.load(tmp_path / "chips" / records[0].path)
    assert arr.shape == (10, 10)
    assert arr.dtype == np.float32


def test_write_chips_writes_jsonl_manifest(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10)

    assert dataset.chip_count == 4
    manifest = tmp_path / "ds" / "manifest.jsonl"
    assert manifest.exists()
    lines = manifest.read_text().strip().splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert first["item_id"] == "test-acq"
    assert first["attribution"].startswith("Contains Umbra")

    summary = dataset.to_dict()
    assert summary["chip_count"] == 4
    assert summary["item_count"] == 1
    assert summary["license"] == "CC-BY-4.0"


def test_write_chips_geojson_manifest(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, manifest="chips.geojson")

    fc = json.loads((tmp_path / "ds" / "chips.geojson").read_text())
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["item_id"] == "test-acq"
    assert dataset.manifest_path.endswith("chips.geojson")


def test_write_chips_parquet_manifest(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("stac_geoparquet")
    pq = pytest.importorskip("pyarrow.parquet")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, manifest="chips.parquet")

    manifest = tmp_path / "ds" / "chips.parquet"
    assert manifest.exists()
    assert dataset.manifest_path.endswith("chips.parquet")

    table = pq.read_table(manifest)
    assert table.num_rows == 4
    # stac-geoparquet writes a geometry column plus the flattened record fields,
    # so a chip set is queryable by DuckDB / geopandas without loading every line.
    assert "geometry" in table.column_names
    rows = table.to_pylist()
    assert {r["item_id"] for r in rows} == {"test-acq"}
    # The chip id is unique per tile (its filename stem), so a dataset's rows
    # don't collide across acquisitions.
    assert len({r["id"] for r in rows}) == 4
    assert rows[0]["asset"] == "GEC"
    assert rows[0]["license"] == "CC-BY-4.0"
    assert rows[0]["attribution"].startswith("Contains Umbra")


def test_write_manifest_parquet_handles_null_datetime(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("stac_geoparquet")
    pq = pytest.importorskip("pyarrow.parquet")
    from umbra_py.chips import chip_item, write_manifest_parquet

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    # An acquisition with no datetime must still produce a valid STAC row
    # (properties.datetime null), not raise.
    item = _item_for(tif)
    item.properties = {k: v for k, v in item.properties.items() if k != "datetime"}
    records = chip_item(item, tmp_path / "chips", chip_size=10)
    assert records and records[0].datetime is None

    out = write_manifest_parquet(records, tmp_path / "m.parquet")
    rows = pq.read_table(out).to_pylist()
    assert len(rows) == 4
    assert rows[0]["datetime"] is None


def test_write_chips_manifest_none_skips_file(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, manifest=None)

    assert dataset.manifest_path is None
    assert not (tmp_path / "ds" / "manifest.jsonl").exists()
    assert dataset.chip_count == 4


def test_invalid_params_raise(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", nodata_corner=False)
    with pytest.raises(ValueError, match="chip_size"):
        chip_item(_item_for(tif), tmp_path / "c", chip_size=0)
    with pytest.raises(ValueError, match="stride"):
        chip_item(_item_for(tif), tmp_path / "c", stride=0)
    with pytest.raises(ValueError, match="fmt"):
        chip_item(_item_for(tif), tmp_path / "c", fmt="jpeg")
    with pytest.raises(ValueError, match="min_valid"):
        chip_item(_item_for(tif), tmp_path / "c", min_valid=1.5)


def test_cli_chips_from_url(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(tif)
    )

    out = tmp_path / "ds"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(out),
            "--chip-size",
            "10",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chip_count"] == 4
    assert payload["items"] == ["cli-acq"]
    assert (out / "manifest.jsonl").exists()


# --- Chipping the complex archive (--asset SICD) -----------------------------
#
# A SICD is complex slant-plane data with no map grid, so it is geocoded through
# `umbra_py.convert` before the same window loop cuts its tiles. The download +
# geocode step is the injectable `preparer`, so every test below runs the whole
# chipping path with no `sarpy`, no network and no multi-gigabyte NITF -- the
# same seam `describe`/`narrate` use for their renders.


def _fake_preparer(cog_path, *, calls=None):
    """A `SicdPreparer` that hands back an already-built raster."""

    def prepare(item, asset, work_dir, conversion):
        if calls is not None:
            calls.append((item.id, asset, str(work_dir), conversion))
        return cog_path

    return prepare


def _make_converted_cog(path, **tags):
    """A small geocoded raster carrying the UMBRA_* provenance tags a real
    `sicd_to_geocoded_cog` output would."""
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.convert import conversion_tags

    tif, bounds, crs = _make_geotiff(path, width=20, height=20, nodata_corner=False)
    with rasterio.open(tif, "r+") as ds:
        ds.update_tags(**conversion_tags(source="scene.nitf", geocoded=True, **tags))
    return tif


def test_sicd_is_chippable_and_goes_through_the_preparer(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import CHIPPABLE_ASSETS, COMPLEX_ASSETS, chip_item

    assert "SICD" in CHIPPABLE_ASSETS
    assert COMPLEX_ASSETS == ("SICD",)

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0", rtc_model="facet")
    calls: list = []
    item = _item_for(tmp_path / "unused.tif", **{"sar:product_type": "SICD"})

    records = chip_item(
        item,
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog, calls=calls),
    )

    # The geometry is the ordinary one: a 20x20 raster in 10 px tiles.
    assert len(records) == 4
    assert [r.asset for r in records] == ["SICD"] * 4
    # ...and the conversion ran exactly once for the acquisition.
    assert len(calls) == 1
    assert calls[0][0] == "test-acq"
    assert calls[0][1] == "SICD"


def test_sicd_records_report_the_processing_that_actually_ran(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="gamma0", rtc_model="facet")
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    # Read back from the raster's own tags, not from the request -- so the
    # manifest reports the processing, not the intent.
    assert {r.calibration for r in records} == {"gamma0"}
    assert {r.rtc_model for r in records} == {"facet"}
    assert records[0].to_dict()["calibration"] == "gamma0"


def test_sicd_records_report_a_subtracted_noise_floor(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # Whether the sensor's own floor came off decides what a dark chip teaches a
    # model, so it rides in the manifest beside the calibration that scaled it.
    cog = _make_converted_cog(
        tmp_path / "geocoded.tif", calibration="gamma0", noise_subtraction="absolute"
    )
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    assert {r.noise_subtraction for r in records} == {"absolute"}
    assert records[0].to_dict()["noise_subtraction"] == "absolute"


# --- What the subtraction did to the batch -----------------------------------
#
# The two diagnostics an inferred floor records are per scene, and `umbra
# convert` prints them for the one raster it writes. A chip run converts many,
# so the same numbers reach a dataset builder as a manifest field per chip and
# one roll-up across the run.


def _preparer_for(mapping):
    """A `SicdPreparer` that hands each acquisition its own prepared raster."""

    def prepare(item, asset, work_dir, conversion):
        return mapping[item.id]

    return prepare


def _sicd_item(item_id, tif_path):
    item = _item_for(tif_path, **{"sar:product_type": "SICD"})
    item.id = item_id
    return item


def test_sicd_records_carry_the_noise_diagnostics(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # Whether the estimate had dark ground to read is a property of the scene a
    # chip came from, so it travels with the chip -- a loader can drop the
    # scenes whose dark tail was ground without opening a raster.
    cog = _make_converted_cog(
        tmp_path / "geocoded.tif",
        noise_subtraction="estimated",
        noise_floor_db=-21.5,
        noise_floored_fraction=0.031,
        noise_floor_margin_db=4.25,
    )
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    assert {r.noise_floored_fraction for r in records} == {0.031}
    assert {r.noise_floor_margin_db for r in records} == {4.25}
    assert records[0].to_dict()["noise_floor_margin_db"] == 4.25


def test_a_measured_floor_reports_no_margin(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # The measured model assumes nothing about the scene, so it has no
    # assumption to report -- a null, not a zero.
    cog = _make_converted_cog(
        tmp_path / "geocoded.tif", noise_subtraction="absolute", noise_floored_fraction=0.004
    )
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    assert records[0].noise_floored_fraction == 0.004
    assert records[0].noise_floor_margin_db is None


def test_noise_summary_counts_scenes_not_chips(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import write_chips

    # Two acquisitions, four chips each: one scene the estimate held on and one
    # it did not. The roll-up answers "how many scenes", because the numbers
    # describe scenes -- counting chips would weight a wide scene more heavily.
    good = _make_converted_cog(
        tmp_path / "good.tif",
        noise_subtraction="estimated-range",
        noise_floored_fraction=0.01,
        noise_floor_margin_db=11.5,
    )
    poor = _make_converted_cog(
        tmp_path / "poor.tif",
        noise_subtraction="estimated-range",
        noise_floored_fraction=0.22,
        noise_floor_margin_db=3.5,
    )
    items = [_sicd_item("acq-good", good), _sicd_item("acq-poor", poor)]

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_preparer_for({"acq-good": good, "acq-poor": poor}),
    )

    assert dataset.chip_count == 8
    noise = dataset.noise
    assert noise is not None
    assert noise.scenes == 2
    assert noise.models == ["estimated-range"]
    assert noise.margin_scenes == 2
    assert noise.low_margin_scenes == 1
    assert noise.min_margin_db == 3.5
    assert noise.max_floored_fraction == 0.22
    # ...and it rides out in the machine-readable summary too.
    assert dataset.to_dict()["noise"]["low_margin_scenes"] == 1


def test_a_run_that_subtracted_nothing_has_no_noise_summary(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import write_chips

    # A GEC run is untouched by the roll-up existing: no subtraction ran, so
    # there is nothing to say and the summary stays out of the output.
    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10)

    assert dataset.noise is None
    assert "noise" not in dataset.to_dict()


def test_cli_chips_reports_the_scenes_the_estimate_should_not_be_trusted_on(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(
        tmp_path / "geocoded.tif",
        noise_subtraction="estimated",
        noise_floored_fraction=0.18,
        noise_floor_margin_db=3.2,
    )
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    real_write_chips = chips_mod.write_chips

    def _spy(items, out_dir, **kwargs):
        return real_write_chips(items, out_dir, preparer=_fake_preparer(cog), **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    args = [
        "chips",
        "http://example.com/item.json",
        "--out",
        str(tmp_path / "ds"),
        "--asset",
        "SICD",
        "--chip-size",
        "10",
        "--subtract-noise",
        "--noise-model",
        "estimated",
    ]
    result = CliRunner().invoke(cli_mod.cli, args)

    assert result.exit_code == 0, result.output
    assert "estimated, subtracted on 1 scene(s)" in result.output
    assert "18.0% of a scene is at the sensor's limit" in result.output
    # The advisory names the count, the threshold and the way out -- it is never
    # a refusal, because a uniformly bright scene is legitimate imagery.
    assert "1 of 1 scene(s) had under 6 dB of margin" in result.output
    assert "--noise-model measured" in result.output

    payload = json.loads(
        CliRunner().invoke(cli_mod.cli, [*args, "--out", str(tmp_path / "ds2"), "--json"]).output
    )
    assert payload["noise"] == {
        "scenes": 1,
        "models": ["estimated"],
        "margin_scenes": 1,
        "low_margin_scenes": 1,
        "margin_warn_db": 6.0,
        "min_margin_db": 3.2,
        "max_floored_fraction": 0.18,
    }


def test_cli_chips_says_nothing_extra_when_the_estimate_held(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    # A wide margin needs no comment: the roll-up says what was subtracted and
    # stops there, so the advisory means something when it does appear.
    cog = _make_converted_cog(
        tmp_path / "geocoded.tif",
        noise_subtraction="estimated",
        noise_floored_fraction=0.01,
        noise_floor_margin_db=14.0,
    )
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    real_write_chips = chips_mod.write_chips
    monkeypatch.setattr(
        chips_mod,
        "write_chips",
        lambda items, out_dir, **kwargs: real_write_chips(
            items, out_dir, preparer=_fake_preparer(cog), **kwargs
        ),
    )

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--chip-size",
            "10",
            "--subtract-noise",
            "--noise-model",
            "estimated",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "subtracted on 1 scene(s)" in result.output
    assert "margin" not in result.output


def test_steps_that_did_not_run_are_null_not_the_string_none(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # conversion_tags writes "none" for a step that was skipped; a manifest
    # field is null instead, so the two conventions are translated once.
    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )
    assert records[0].calibration is None
    assert records[0].rtc_model is None
    assert records[0].noise_subtraction is None


def test_sicd_chips_carry_the_conversion_provenance_in_the_file(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item
    from umbra_py.convert import read_conversion_tags

    cog = _make_converted_cog(
        tmp_path / "geocoded.tif", calibration="sigma0", rtc_model="gamma", dem="glo30.tif"
    )
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    chip = tmp_path / "chips" / records[0].path
    prov = read_conversion_tags(chip)
    # A chip says what its pixel values are without the manifest beside it.
    assert prov["calibration"] == "sigma0"
    assert prov["rtc_model"] == "gamma"
    assert prov["dem"] == "glo30.tif"
    assert prov["license"] == "CC-BY-4.0"
    with rasterio.open(chip) as ds:
        # The chip's own tags survive alongside the inherited ones.
        assert ds.tags()["item_id"] == "test-acq"


def test_amplitude_chips_are_untouched_by_the_provenance_plumbing(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)

    assert records[0].calibration is None
    assert records[0].rtc_model is None
    with rasterio.open(tmp_path / "chips" / records[0].path) as ds:
        # A GEC is the published product, not something this library made.
        assert not [k for k in ds.tags() if k.startswith("UMBRA_")]


def test_temporary_work_dir_is_removed_after_chipping(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    calls: list = []
    chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog, calls=calls),
    )
    # No work_dir given -> the scene's bytes don't outlive the run, so a long
    # series stays bounded to one acquisition on disk.
    assert not Path(calls[0][2]).exists()


def test_named_work_dir_is_used_and_kept(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    work = tmp_path / "work"
    calls: list = []
    chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        work_dir=work,
        preparer=_fake_preparer(cog, calls=calls),
    )
    assert Path(calls[0][2]) == work
    assert work.exists()


def test_conversion_cache_key_tracks_every_setting():
    from umbra_py.chips import SicdConversion

    base = SicdConversion()
    assert base.cache_key() == SicdConversion().cache_key()
    # Each setting that changes the pixels changes the cached filename, so a
    # re-run with different processing never chips the previous product.
    for field_name, value in [
        ("calibration", "sigma0"),
        ("noise_subtract", True),
        ("rtc", True),
        ("rtc_model", "facet"),
        ("dem", "glo30.tif"),
        ("geoid", "egm96.tif"),
        ("resolution", 1e-5),
        ("resampling", "cubic"),
        ("gcp_grid", 21),
        ("projection_type", "PLANE"),
        ("rtc_reference_deg", 30.0),
    ]:
        assert dataclasses.replace(base, **{field_name: value}).cache_key() != base.cache_key()


def test_prepare_sicd_reuses_an_already_geocoded_scene(tmp_path):
    from umbra_py.chips import SicdConversion, _prepare_sicd, _safe_slug

    conversion = SicdConversion(calibration="sigma0")
    work = tmp_path / "work"
    work.mkdir()
    cached = work / f"{_safe_slug('test-acq')}.{conversion.cache_key()}.tif"
    cached.write_bytes(b"not really a tif")

    item = _item_for(tmp_path / "unused.tif")
    # Returns the cached COG without downloading or converting anything -- which
    # is what makes a re-run over a large chip set cheap.
    assert _prepare_sicd(item, "SICD", work, conversion) == cached


def test_write_chips_reports_the_conversion_it_used(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    conversion = SicdConversion(calibration="sigma0")
    dataset = write_chips(
        [_item_for(tmp_path / "unused.tif")],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=conversion,
        preparer=_fake_preparer(cog),
    )
    assert dataset.chip_count == 4
    assert dataset.to_dict()["conversion"]["calibration"] == "sigma0"


def test_write_chips_defaults_the_conversion_for_a_complex_asset(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    dataset = write_chips(
        [_item_for(tmp_path / "unused.tif")],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )
    # The summary reports the settings that ran, not the (absent) request.
    assert dataset.to_dict()["conversion"]["projection_type"] == "HAE"


def test_amplitude_dataset_summary_is_unchanged(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10)
    assert "conversion" not in dataset.to_dict()


def test_cli_chips_sicd_builds_the_conversion(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="gamma0", rtc_model="facet")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    real_write_chips = chips_mod.write_chips
    captured: dict = {}

    def _spy(items, out_dir, **kwargs):
        captured.update(kwargs)
        return real_write_chips(items, out_dir, preparer=_fake_preparer(cog), **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--chip-size",
            "10",
            "--dem",
            "auto",
            "--rtc",
            "--rtc-model",
            "facet",
            "--calibrate",
            "gamma0",
            "--subtract-noise",
            "--noise-model",
            "estimated",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    conversion = captured["conversion"]
    assert conversion.dem == "auto"
    assert conversion.rtc is True
    assert conversion.rtc_model == "facet"
    assert conversion.calibration == "gamma0"
    assert conversion.noise_subtract is True
    assert conversion.noise_model == "estimated"
    payload = json.loads(result.output)
    assert payload["conversion"]["calibration"] == "gamma0"


def test_cli_chips_defaults_to_the_measured_noise_floor(tmp_path, monkeypatch):
    """--subtract-noise alone means what it meant before --noise-model existed."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    captured: dict = {}
    real_write_chips = chips_mod.write_chips

    def _spy(items, out_dir, **kwargs):
        captured.update(kwargs)
        return real_write_chips(items, out_dir, preparer=_fake_preparer(cog), **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--chip-size",
            "10",
            "--subtract-noise",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["conversion"].noise_model == "measured"


def test_conversion_cache_key_separates_a_measured_floor_from_an_inferred_one():
    from umbra_py.chips import SicdConversion

    # The work_dir cache is keyed on the settings, so a run that estimated the
    # floor must never hand back the COG a run that measured it left behind:
    # the pixels differ and nothing downstream would notice.
    measured = SicdConversion(noise_subtract=True, noise_model="measured")
    estimated = SicdConversion(noise_subtract=True, noise_model="estimated")
    assert measured.cache_key() != estimated.cache_key()


def test_cli_rejects_conversion_flags_on_an_amplitude_asset(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--calibrate",
            "sigma0",
        ],
    )
    # A silently ignored --calibrate would produce an uncalibrated dataset the
    # caller believes is calibrated, so it is a usage error instead.
    assert result.exit_code != 0
    assert "--calibrate" in result.output
    assert "--asset SICD" in result.output


# --- Chipping one area of interest (bbox= / --clip-bbox) ---------------------


def _lonlat_window(tif, row0, col0, row_stop, col_stop):
    """The lon/lat bbox of a pixel window of a written raster."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.warp import transform_bounds

    with rasterio.open(tif) as ds:
        left, top = ds.transform * (col0, row0)
        right, bottom = ds.transform * (col_stop, row_stop)
        return transform_bounds(ds.crs, "EPSG:4326", left, bottom, right, top)


def test_chip_item_bbox_tiles_only_the_area_of_interest(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    # The bottom-right quadrant: one 10 px tile out of the four the whole raster
    # would give.
    bbox = _lonlat_window(tif, 10, 10, 20, 20)

    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, bbox=bbox)

    assert len(records) == 1
    # Rows/columns are numbered from the window's own corner, not the raster's.
    assert (records[0].row, records[0].col) == (0, 0)
    # The window starts at the quadrant, to within the pixel the bbox is rounded
    # outward to (the raster is projected, so the lon/lat request is reprojected
    # back and lands a hair off a pixel edge).
    col_off, row_off, width, height = records[0].window
    assert (width, height) == (10, 10)
    assert abs(row_off - 10) <= 1 and abs(col_off - 10) <= 1
    # And the chip really is (about) the ground that was asked for: a 10 px
    # tile of a 10 m raster, so a pixel of slack is ~1e-4 degrees.
    west, south, east, north = records[0].bbox
    assert west == pytest.approx(bbox[0], abs=2e-4)
    assert north == pytest.approx(bbox[3], abs=2e-4)


def test_chip_item_without_bbox_still_tiles_the_whole_raster(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    assert len(chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)) == 4


def test_chip_item_bbox_off_the_raster_errors(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    with pytest.raises(ValueError, match="does not overlap"):
        chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, bbox=(10.0, 10.0, 10.5, 10.5))


def test_chip_item_bbox_becomes_the_sicd_conversion_clip(tmp_path):
    """The expensive half: a complex scene is geocoded over the area, not whole."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    bbox = _lonlat_window(cog, 10, 10, 20, 20)
    calls: list = []

    chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        bbox=bbox,
        preparer=_fake_preparer(cog, calls=calls),
    )

    conversion = calls[0][3]
    assert conversion.bbox == tuple(float(v) for v in bbox)


def test_conversion_cache_key_tracks_the_clip(tmp_path):
    from umbra_py.chips import SicdConversion

    base = SicdConversion()
    clipped = dataclasses.replace(base, bbox=(-100.0, 39.0, -99.0, 40.0))
    # A clipped conversion is a different product, so it never reuses (or is
    # reused as) the whole-scene COG cached in --work-dir.
    assert clipped.cache_key() != base.cache_key()
    assert (
        clipped.cache_key()
        == dataclasses.replace(base, bbox=(-100.0, 39.0, -99.0, 40.0)).cache_key()
    )


def test_cli_chips_clip_bbox_reaches_the_writer(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    bbox = _lonlat_window(tif, 10, 10, 20, 20)
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json",
        lambda url: {"id": "cli-acq", "assets": {"GEC": {"href": str(tif)}}},
    )
    real_write_chips = chips_mod.write_chips
    captured: dict = {}

    def _spy(items, out_dir, **kwargs):
        captured.update(kwargs)
        return real_write_chips(items, out_dir, **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--chip-size",
            "10",
            "--clip-bbox",
            ",".join(str(v) for v in bbox),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["bbox"] == pytest.approx(bbox)
    assert json.loads(result.output)["chip_count"] == 1
