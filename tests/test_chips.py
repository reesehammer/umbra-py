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
import responses

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


def test_sicd_records_report_the_speckle_filter_and_its_window(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # A filtered chip's *resolution* is its window, not its pixel size, so a
    # training loader has to be able to read that off the manifest -- a model
    # trained on 5x5-averaged tiles cannot learn to see what they averaged away.
    cog = _make_converted_cog(
        tmp_path / "geocoded.tif",
        calibration="gamma0",
        speckle_filter="lee",
        speckle_window=5,
        speckle_enl_after=18.0,
    )
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    assert {r.speckle_filter for r in records} == {"lee"}
    # An int, because a loader comparing it against a chip size should not have
    # to think about 5.0 against 5.
    assert {r.speckle_window for r in records} == {5}
    assert records[0].to_dict()["speckle_window"] == 5


def test_unfiltered_sicd_records_carry_no_speckle_fields(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="gamma0")
    records = chip_item(
        _item_for(tmp_path / "unused.tif"),
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        preparer=_fake_preparer(cog),
    )

    # ``conversion_tags`` writes "none" for a step that did not run; the manifest
    # convention is ``None``, so the translation happens once on the way in.
    assert records[0].speckle_filter is None
    assert records[0].speckle_window is None


def test_speckle_settings_reach_the_conversion(tmp_path):
    from umbra_py.chips import SicdConversion, _prepare_sicd

    seen = {}

    def fake_geocode(src, dst, **kwargs):
        seen.update(kwargs)
        Path(dst).write_bytes(b"cog")
        return Path(dst)

    import umbra_py.convert as convert_mod
    import umbra_py.download as download_mod

    original_geocode = convert_mod.sicd_to_geocoded_cog
    original_download = download_mod.download_asset
    convert_mod.sicd_to_geocoded_cog = fake_geocode
    download_mod.download_asset = lambda item, asset, work_dir: Path(work_dir) / "scene.ntf"
    try:
        _prepare_sicd(
            _item_for(tmp_path / "unused.tif"),
            "SICD",
            tmp_path / "work",
            SicdConversion(speckle_filter="boxcar", speckle_window=9),
        )
    finally:
        convert_mod.sicd_to_geocoded_cog = original_geocode
        download_mod.download_asset = original_download

    # The chipper is a handle on the conversion pipeline, not a second
    # implementation of it: the flags are passed straight down.
    assert seen["speckle_filter"] == "boxcar"
    assert seen["speckle_window"] == 9


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
        ("speckle_filter", "lee"),
        ("speckle_window", 7),
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


# --- Speckle-filtering the published amplitude rasters ------------------------
#
# `umbra convert --speckle-filter` reaches the complex archive and `umbra stack
# --speckle-filter` reaches a datacube, but the chipper's own loader -- the one
# that turns Umbra's *published* GEC rasters into a training set -- had no route
# to averaging speckle at all. These cover the two things that made the tile
# loop the hard place to put it: a window straddling a tile boundary, and
# `lee`'s speckle parameter being a property of the product rather than of the
# 512 pixels it happens to be looking at.


def _speckle_scene(path, *, width=96, height=96, seed=7):
    """A synthetic single-look scene: exponential power over two surfaces.

    Speckle on one look is exponentially distributed in power, so this is what
    an ENL estimate should read 1.0 on -- and the bright/dark step is the
    structure `lee` is supposed to keep and `boxcar` is not.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    rng = np.random.default_rng(seed)
    truth = np.where(np.arange(width)[None, :] < width // 2, 1.0, 6.0) * np.ones((height, 1))
    data = np.sqrt(rng.exponential(truth)).astype("float32")
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
        dst.write(data, 1)
    return path, data


@pytest.mark.parametrize("name", ["boxcar", "lee"])
def test_a_filtered_tile_equals_the_whole_scene_filter_over_the_same_ground(tmp_path, name):
    """The halo claim, which is the whole reason this can be done tile by tile.

    A window centred near a tile's edge needs pixels the neighbouring tile holds.
    Read the tile alone and those pixels are missing, so an edge pixel averages a
    truncated window -- and two *overlapping* tiles then disagree about the same
    ground, which is a seam a model would learn. Reading a half-window halo and
    cropping after the filter makes each tile pixel-identical to that region of
    the scene filtered whole, which is what this asserts (with stride < chip_size,
    so tiles do overlap).
    """
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item
    from umbra_py.convert import _filter_speckle

    tif, data = _speckle_scene(tmp_path / "scene.tif")
    records = chip_item(
        _item_for(tif),
        tmp_path / "chips",
        chip_size=32,
        stride=16,
        speckle_filter=name,
        speckle_window=5,
    )
    assert len(records) == 25  # 5x5 overlapping tiles

    # The same filter over the whole scene, told the same looks the chipper read
    # for the acquisition -- so what is being compared is the windowing, not the
    # parameter.
    looks = records[0].speckle_looks
    whole, _ = _filter_speckle(
        data, decibels=False, name=name, window=5, looks=looks if looks else 1.0
    )
    for record in records:
        col_off, row_off, width, height = record.window
        with rasterio.open(tmp_path / "chips" / record.path) as src:
            tile = src.read(1)
        expected = whole[row_off : row_off + height, col_off : col_off + width]
        assert np.array_equal(tile, expected)


def test_lee_reads_its_speckle_parameter_once_for_the_acquisition(tmp_path):
    """Not once per tile, which is what would make the filter vary across a scene.

    `lee`'s ``looks`` says how variable speckle alone would make a window, so it
    decides where the filter smooths and where it keeps the pixel. It is a
    property of the *product's* processing, so a tile that read it off its own
    pixels would filter one over water differently from the one beside it over a
    city -- for no reason in the data.
    """
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _ = _speckle_scene(tmp_path / "scene.tif", width=128, height=128)
    records = chip_item(
        _item_for(tif), tmp_path / "chips", chip_size=32, speckle_filter="lee", speckle_window=5
    )

    assert len({r.speckle_looks for r in records}) == 1
    # Clamped at single-look: no product has fewer looks than one, so a lower
    # read is the estimator meeting texture rather than physics.
    assert records[0].speckle_looks >= 1.0


def test_boxcar_needs_no_looks_and_reports_none(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=32, speckle_filter="boxcar")

    assert records[0].speckle_filter == "boxcar"
    assert records[0].speckle_looks is None


def test_a_filtered_amplitude_record_reports_what_the_window_achieved(tmp_path):
    """The window says how many *pixels* were averaged; the ENL pair says how
    many independent *measurements* that was, which is the number that matters."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    records = chip_item(
        _item_for(tif),
        tmp_path / "chips",
        chip_size=32,
        speckle_filter="boxcar",
        speckle_window=5,
    )

    record = records[0]
    assert record.speckle_filter == "boxcar"
    assert record.speckle_window == 5
    # Single-look imagery sits at about 1.0 before, and a 5x5 boxcar over truly
    # independent samples buys most of its 25 pixels back as looks.
    assert record.speckle_enl_before == pytest.approx(1.0, abs=0.3)
    assert record.speckle_enl_after > 5.0 * record.speckle_enl_before
    # A per-scene diagnostic, so every tile of the acquisition carries the same.
    assert len({r.speckle_enl_after for r in records}) == 1


def test_a_filtered_amplitude_chip_carries_the_umbra_tags_in_the_file(tmp_path):
    """`umbra convert`'s own vocabulary, not a second one -- so `to_stack`'s
    refusal to difference a filtered pass against an unfiltered one, and anyone
    running gdalinfo on a chip, both work on it unchanged."""
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    records = chip_item(
        _item_for(tif), tmp_path / "chips", chip_size=32, speckle_filter="lee", speckle_window=5
    )

    with rasterio.open(tmp_path / "chips" / records[0].path) as src:
        tags = src.tags()
    assert tags["UMBRA_SPECKLE_FILTER"] == "lee"
    assert tags["UMBRA_SPECKLE_WINDOW"] == "5"
    assert "UMBRA_SPECKLE_ENL_BEFORE" in tags
    assert "UMBRA_SPECKLE_LOOKS" in tags


def test_an_unfiltered_amplitude_run_is_unchanged(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item, write_chips

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=32)

    assert records[0].speckle_filter is None
    assert records[0].speckle_window is None
    assert records[0].speckle_enl_before is None
    assert records[0].speckle_looks is None

    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=32, manifest=None)
    # Absent from the summary entirely, so an ordinary run's --json payload is
    # unchanged by this feature existing.
    assert dataset.speckle is None
    assert "speckle" not in dataset.to_dict()


def test_the_filter_does_not_change_which_tiles_are_dropped(tmp_path):
    """A filter changes values, not the mask, so min_valid decides the same way
    either side of it -- the drop is computed on the tile as read."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20)
    plain = chip_item(_item_for(tif), tmp_path / "a", chip_size=10, min_valid=0.9)
    filtered = chip_item(
        _item_for(tif), tmp_path / "b", chip_size=10, min_valid=0.9, speckle_filter="boxcar"
    )

    assert [(r.row, r.col) for r in plain] == [(r.row, r.col) for r in filtered]
    assert [r.valid_fraction for r in plain] == [r.valid_fraction for r in filtered]


def test_nodata_is_excluded_from_its_neighbours_windows(tmp_path):
    """Rather than averaged in as a zero, which would drag an edge tile's values
    toward a return the sensor never got."""
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    # The top-left 5x5 block is non-positive (`_make_geotiff`'s nodata corner).
    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, speckle_filter="boxcar")

    corner = next(r for r in records if (r.row, r.col) == (0, 0))
    with rasterio.open(tmp_path / "chips" / corner.path) as src:
        tile = src.read(1)
    # The invalid block stays invalid -- a filter has no measurement there to
    # improve, and filling it from neighbours would invent ground.
    assert np.isnan(tile[:5, :5]).all()
    # And a pixel just outside it is finite and positive rather than dragged to
    # zero by five columns of nothing.
    assert np.isfinite(tile[0, 5]) and tile[0, 5] > 0


def test_an_even_speckle_window_is_refused_at_the_call(tmp_path):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif")
    with pytest.raises(ValueError, match="odd integer"):
        chip_item(
            _item_for(tif),
            tmp_path / "chips",
            chip_size=10,
            speckle_filter="boxcar",
            speckle_window=4,
        )
    with pytest.raises(ValueError, match="Unknown speckle_filter"):
        chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, speckle_filter="frost")
    # Nothing was read, so nothing was written.
    assert not (tmp_path / "chips").exists() or not list((tmp_path / "chips").glob("*.tif"))


def test_filtering_an_already_filtered_raster_is_refused(tmp_path):
    """Two averagings leave a resolution neither window names, so the record a
    chip would carry -- and anything a model learns from it -- would understate
    what the smoothing cost. The same rule `to_stack` applies to a cube."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    cog = _make_converted_cog(tmp_path / "filtered.tif", speckle_filter="lee", speckle_window=3)
    item = _item_for(tmp_path / "unused.tif")
    item.asset_href = lambda asset="GEC": str(cog)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="already-filtered"):
        chip_item(item, tmp_path / "chips", chip_size=10, speckle_filter="boxcar")


def test_a_complex_asset_routes_the_filter_into_the_conversion(tmp_path):
    """A SICD is filtered in the radar's own image space, before geocoding, where
    speckle is one independent sample per pixel -- so the request goes there
    rather than to the tile loop."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", speckle_filter="lee", speckle_window=7)
    dataset = write_chips(
        [_item_for(tmp_path / "unused.tif")],
        tmp_path / "chips",
        asset="SICD",
        chip_size=10,
        manifest=None,
        speckle_filter="lee",
        speckle_window=7,
        preparer=_fake_preparer(cog),
    )

    assert isinstance(dataset.conversion, SicdConversion)
    assert dataset.conversion.speckle_filter == "lee"
    assert dataset.conversion.speckle_window == 7
    # And the record still comes off the converted raster's own tags, so it
    # reports the processing that ran rather than the one requested.
    assert dataset.records[0].speckle_filter == "lee"


def test_two_conflicting_speckle_requests_are_refused(tmp_path):
    """Only the caller knows which they meant, so it is not silently resolved."""
    from umbra_py.chips import SicdConversion, write_chips

    with pytest.raises(ValueError, match="Conflicting speckle filters"):
        write_chips(
            [_item_for(tmp_path / "unused.tif")],
            tmp_path / "chips",
            asset="SICD",
            manifest=None,
            speckle_filter="boxcar",
            conversion=SicdConversion(speckle_filter="lee"),
        )


def test_speckle_summary_counts_scenes_not_chips(tmp_path):
    """The diagnostics describe the scene each tile was cut from, so counting
    chips would weight a wide scene more heavily than a narrow one."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from umbra_py.chips import write_chips

    first, _ = _speckle_scene(tmp_path / "a.tif", seed=1)
    second, _ = _speckle_scene(tmp_path / "b.tif", seed=2)
    items = [_item_for(first), _item_for(second)]
    items[1].id = "second-acq"

    dataset = write_chips(
        items, tmp_path / "ds", chip_size=32, manifest=None, speckle_filter="boxcar"
    )

    summary = dataset.speckle
    assert summary is not None
    assert dataset.chip_count > summary.scenes
    assert summary.scenes == 2
    assert summary.filters == ["boxcar"]
    assert summary.windows == [5]
    assert summary.median_gain > 1.0
    assert dataset.to_dict()["speckle"]["scenes"] == 2


def test_sample_offsets_spread_and_collapse(tmp_path):
    from umbra_py.chips import _sample_offsets

    # A span no wider than one window is sampled once rather than nine times over.
    assert _sample_offsets(0, 400, 512, 3) == [0]
    # Otherwise evenly spread, first at the start and last flush with the end.
    offsets = _sample_offsets(0, 2048, 512, 3)
    assert offsets == [0, 768, 1536]
    assert offsets[-1] + 512 == 2048


def test_cli_chips_filters_an_amplitude_asset(tmp_path, monkeypatch):
    """--speckle-filter used to be rejected on GEC as a SICD-only flag; the
    published rasters are exactly what it is most needed on."""
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json",
        lambda url: {"id": "cli-acq", "assets": {"GEC": {"href": str(tif)}}},
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--chip-size",
            "32",
            "--speckle-filter",
            "boxcar",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["speckle"]["filters"] == ["boxcar"]
    assert payload["speckle"]["scenes"] == 1


def test_cli_chips_reports_what_the_window_bought(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json",
        lambda url: {"id": "cli-acq", "assets": {"GEC": {"href": str(tif)}}},
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--chip-size",
            "32",
            "--speckle-filter",
            "boxcar",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "speckle: boxcar over 5x5, on 1 scene(s)" in result.output
    assert "equivalent looks up by" in result.output


def test_cli_chips_says_nothing_about_speckle_when_none_ran(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _ = _speckle_scene(tmp_path / "scene.tif")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json",
        lambda url: {"id": "cli-acq", "assets": {"GEC": {"href": str(tif)}}},
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--chip-size",
            "32",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "speckle" not in result.output


def test_cli_chips_rejects_an_even_speckle_window(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json",
        lambda url: {"id": "cli-acq", "assets": {"GEC": {"href": str(tif)}}},
    )
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--speckle-filter",
            "boxcar",
            "--speckle-window",
            "4",
        ],
    )

    assert result.exit_code != 0
    assert "--speckle-window" in result.output


# --------------------------------------------------------------------------- #
# Carrying on past an acquisition whose metadata cannot support the request.
# --------------------------------------------------------------------------- #
# A batch over a mixed archive used to die on the first product that carries no
# `Radiometric` block, taking every scene already chipped with it. The refusal
# is right -- an invented scale factor is indistinguishable from a real one --
# so what changed is that it now has a type a batch can act on.


def _refusing_preparer(cog_path, *, refuse, message="SICD carries no Radiometric metadata."):
    """A `SicdPreparer` that refuses the named acquisitions the way convert does."""
    from umbra_py.exceptions import UnsupportedMeasurementError

    def prepare(item, asset, work_dir, conversion):
        if item.id in refuse:
            raise UnsupportedMeasurementError(message, hint="Try --noise-model estimated.")
        return cog_path

    return prepare


def test_a_batch_stops_on_an_unsupported_product_by_default(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips
    from umbra_py.exceptions import UnsupportedMeasurementError

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    items = [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog), _sicd_item("acq-c", cog)]

    with pytest.raises(UnsupportedMeasurementError):
        write_chips(
            items,
            tmp_path / "ds",
            asset="SICD",
            chip_size=10,
            preparer=_refusing_preparer(cog, refuse={"acq-b"}),
        )


def test_skip_unsupported_keeps_the_rest_and_says_what_it_left_out(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    items = [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog), _sicd_item("acq-c", cog)]

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse={"acq-b"}),
        skip_unsupported=True,
    )

    # The two acquisitions that could answer are in the dataset...
    assert sorted({r.item_id for r in dataset.records}) == ["acq-a", "acq-c"]
    assert dataset.chip_count == 8
    # ...and the one that could not is in the result rather than only in a log.
    assert [s.item_id for s in dataset.skipped] == ["acq-b"]
    skipped = dataset.skipped[0]
    assert "Radiometric" in skipped.reason
    assert skipped.hint == "Try --noise-model estimated."
    assert skipped.datetime == "2024-02-08T12:00:00+00:00"
    # The manifest is still written, for the scenes that made it.
    assert dataset.manifest_path is not None
    lines = Path(dataset.manifest_path).read_text().strip().splitlines()
    assert len(lines) == dataset.chip_count


def test_the_skipped_block_is_absent_from_a_run_that_skipped_nothing(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_sicd_item("acq-a", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse=set()),
        skip_unsupported=True,
    )

    assert dataset.skipped == ()
    assert "skipped" not in dataset.to_dict()


def test_skipped_acquisitions_reach_the_json_summary(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse={"acq-b"}),
        skip_unsupported=True,
    )

    payload = dataset.to_dict()
    assert payload["skipped_count"] == 1
    assert payload["skipped"][0]["item_id"] == "acq-b"
    assert "Radiometric" in payload["skipped"][0]["reason"]
    # The chips that were written are counted as usual, so a consumer reading
    # only `chip_count` is not silently told about a smaller dataset.
    assert payload["chip_count"] == 4
    assert payload["items"] == ["acq-a"]


def test_the_skipped_acquisitions_are_written_beside_the_manifest(tmp_path):
    """The dataset on disk states its hole, not just the run that built it."""
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse={"acq-b"}),
        skip_unsupported=True,
    )

    sidecar = tmp_path / "ds" / "skipped.jsonl"
    assert dataset.skipped_path == str(sidecar)
    rows = [json.loads(line) for line in sidecar.read_text().strip().splitlines()]
    assert len(rows) == 1
    # The file says the same thing `ChipDataset.skipped` does, in the product's
    # own words -- which is the whole point of writing it.
    assert rows[0]["item_id"] == "acq-b"
    assert "Radiometric" in rows[0]["reason"]
    assert rows[0]["hint"] == "Try --noise-model estimated."
    assert rows[0]["datetime"] == "2024-02-08T12:00:00+00:00"
    assert rows[0]["stage"] == "conversion"
    # And it is a sidecar: the chip manifest keeps its one-row-per-chip schema.
    manifest_rows = Path(dataset.manifest_path).read_text().strip().splitlines()
    assert len(manifest_rows) == dataset.chip_count
    assert all(json.loads(line)["item_id"] == "acq-a" for line in manifest_rows)
    assert dataset.to_dict()["skipped_manifest"] == str(sidecar)


def test_a_run_that_skipped_nothing_writes_no_sidecar(tmp_path):
    """A clean run leaves exactly the files it left before this existed."""
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_sicd_item("acq-a", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse=set()),
        skip_unsupported=True,
    )

    assert dataset.skipped_path is None
    assert not (tmp_path / "ds" / "skipped.jsonl").exists()
    assert "skipped_manifest" not in dataset.to_dict()


def test_a_preflighted_drop_reaches_the_sidecar_naming_the_stage(tmp_path):
    """A cheaply-found hole is written like an expensively-found one."""
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    products = _write_products(tmp_path)
    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_nitf_item(name, path) for name, path in products.items()],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=lambda item, asset, work_dir, conversion: cog,
        conversion=SicdConversion(calibration="sigma0"),
        preflight=True,
    )

    rows = [
        json.loads(line) for line in Path(dataset.skipped_path).read_text().strip().splitlines()
    ]
    assert [r["item_id"] for r in rows] == ["acq-b"]
    # `stage` is the one field the two routes to a hole do not share, so it is
    # the one a loader needs to tell "never downloaded" from "downloaded and
    # refused" -- and it survives into the file.
    assert rows[0]["stage"] == "preflight"


def test_the_sidecar_can_be_suppressed_and_follows_the_manifest(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    items = [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog)]
    kwargs = {
        "asset": "SICD",
        "chip_size": 10,
        "preparer": _refusing_preparer(cog, refuse={"acq-b"}),
        "skip_unsupported": True,
    }

    off = write_chips(items, tmp_path / "off", skipped_manifest=None, **kwargs)
    assert off.skipped_path is None
    assert not (tmp_path / "off" / "skipped.jsonl").exists()
    # The hole is still in the result -- only the file was declined.
    assert [s.item_id for s in off.skipped] == ["acq-b"]

    # `manifest=None` means "collect the records, write nothing", and that is
    # still true: the sidecar is a description of the dataset, not of the run.
    none = write_chips(items, tmp_path / "none", manifest=None, **kwargs)
    assert none.skipped_path is None
    assert list((tmp_path / "none").glob("*.jsonl")) == []

    named = write_chips(items, tmp_path / "named", skipped_manifest="holes.jsonl", **kwargs)
    assert named.skipped_path == str(tmp_path / "named" / "holes.jsonl")


def test_a_batch_still_stops_on_an_error_that_is_not_about_the_product(tmp_path):
    """`skip_unsupported` is not a blanket `except Exception`."""
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips
    from umbra_py.exceptions import DownloadError

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")

    def prepare(item, asset, work_dir, conversion):
        if item.id == "acq-b":
            raise DownloadError("connection reset")
        return cog

    with pytest.raises(DownloadError):
        write_chips(
            [_sicd_item("acq-a", cog), _sicd_item("acq-b", cog)],
            tmp_path / "ds",
            asset="SICD",
            chip_size=10,
            preparer=prepare,
            skip_unsupported=True,
        )


def test_cli_chips_skip_unsupported_reports_the_acquisitions_it_left_out(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    real_write_chips = chips_mod.write_chips

    def _spy(items, out_dir, **kwargs):
        return real_write_chips(
            items,
            out_dir,
            preparer=_refusing_preparer(cog, refuse={"cli-acq"}),
            **kwargs,
        )

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
        "--calibrate",
        "sigma0",
    ]
    refused = CliRunner().invoke(cli_mod.cli, args)
    assert refused.exit_code != 0

    result = CliRunner().invoke(cli_mod.cli, [*args, "--skip-unsupported"])
    assert result.exit_code == 0, result.output
    assert "Skipped 1 acquisition(s)" in result.output
    assert "cli-acq" in result.output
    assert "hint:" in result.output
    # And the run points at the file that will still say so when nobody is
    # watching the console.
    sidecar = tmp_path / "ds" / "skipped.jsonl"
    assert f"skipped -> {sidecar}" in result.output
    assert json.loads(sidecar.read_text().strip())["item_id"] == "cli-acq"


# --------------------------------------------------------------------------- #
# Asking the archive before paying for it (`write_chips(preflight=True)`).
#
# The fixtures are real NITF bytes from `test_preflight`, so what is under test
# is the same range-read the standalone command does -- wired into the batch it
# exists to save.
# --------------------------------------------------------------------------- #


def _nitf_item(item_id, nitf_path, when="2024-02-08T12:00:00Z"):
    """An item whose SICD asset is a real (small) NITF the preflight can read."""
    item = UmbraItem(
        id=item_id,
        properties={
            "datetime": when,
            "platform": "Umbra-08",
            "sar:polarizations": ["VV"],
            "sar:product_type": "SICD",
        },
    )
    item.asset_href = lambda asset="SICD": str(nitf_path)  # type: ignore[method-assign]
    return item


def _write_products(tmp_path):
    """Two products that declare a calibration and one that declares nothing."""
    from .test_preflight import _CALIBRATED, _UNCALIBRATED, build_nitf

    paths = {}
    for name, xml in (
        ("acq-a", _CALIBRATED),
        ("acq-b", _UNCALIBRATED),
        ("acq-c", _CALIBRATED),
    ):
        path = tmp_path / f"{name}.nitf"
        path.write_bytes(build_nitf(xml))
        paths[name] = path
    return paths


def _recording_preparer(cog_path, seen):
    """A `SicdPreparer` that records which acquisitions were actually prepared."""

    def prepare(item, asset, work_dir, conversion):
        seen.append(item.id)
        return cog_path

    return prepare


def test_preflight_drops_the_unanswerable_passes_before_downloading_them(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    items = [_nitf_item(name, path) for name, path in products.items()]
    prepared: list[str] = []

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, prepared),
        preflight=True,
    )

    # The product that could not answer never reached the preparer -- which is
    # the whole claim: the refusal cost its header, not a download.
    assert prepared == ["acq-a", "acq-c"]
    assert sorted({r.item_id for r in dataset.records}) == ["acq-a", "acq-c"]
    # ...and the dataset states its hole, the way a survived refusal does.
    assert [s.item_id for s in dataset.skipped] == ["acq-b"]
    assert dataset.skipped[0].stage == "preflight"
    assert "Radiometric" in dataset.skipped[0].reason
    assert dataset.skipped[0].datetime == "2024-02-08T12:00:00+00:00"


def test_a_flattening_run_preflights_the_collection_geometry(tmp_path):
    """`--rtc` is a metadata-dependent correction too, so the run's own settings
    put it in the question the preflight asks -- and a pass that states no
    SCPCOA is dropped before the download, the DEM fetch and the warp it would
    otherwise have refused after."""
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    from .test_preflight import _UNCALIBRATED, _WITH_GEOMETRY, build_nitf

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    paths = {}
    for name, xml in (("acq-geo", _WITH_GEOMETRY), ("acq-bare", _UNCALIBRATED)):
        path = tmp_path / f"{name}.nitf"
        path.write_bytes(build_nitf(xml))
        paths[name] = path
    items = [_nitf_item(name, path) for name, path in paths.items()]
    prepared: list[str] = []

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        # `rtc` needs a DEM at conversion time; the preflight asks only what the
        # metadata answers, which is the point -- the geometry question is free.
        conversion=SicdConversion(rtc=True, dem="dem.tif"),
        preparer=_recording_preparer(cog, prepared),
        preflight=True,
    )

    assert prepared == ["acq-geo"]
    assert [s.item_id for s in dataset.skipped] == ["acq-bare"]
    assert "SCPCOA" in dataset.skipped[0].reason
    assert dataset.skipped[0].stage == "preflight"


def test_the_preflight_summary_reports_what_the_check_cost_and_saved(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    items = [_nitf_item(name, path) for name, path in products.items()]

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, []),
        preflight=True,
    )

    summary = dataset.preflight
    assert summary is not None
    assert (summary.checked, summary.supported, summary.skipped) == (3, 2, 0 + 1)
    assert summary.unreadable == 0
    # Only the dropped product is a saving -- the two that were kept get
    # downloaded anyway, so their header reads are overhead rather than avoided.
    assert summary.product_bytes_skipped == products["acq-b"].stat().st_size
    # And asking cost a fraction of the one product it removed.
    assert 0 < summary.bytes_read < sum(p.stat().st_size for p in products.values())


@responses.activate
def test_an_unreachable_acquisition_is_chipped_rather_than_silently_dropped(tmp_path):
    """A read that failed on the *wire* is not a product saying anything.

    It is the one failure a preflight cannot turn into a verdict, so the pass
    stays in the run and the batch finds out the expensive way -- the cautious
    branch, deliberately, because dropping a scene over a blip would put a hole
    in a dataset that the archive never had.
    """
    pytest.importorskip("numpy")
    import requests

    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    url = "https://example.com/flaky_SICD.nitf"
    # The session's own retries ride out a 5xx, so the failure under test is the
    # one that outlives them: a connection that never completes.
    responses.add(responses.GET, url, body=requests.ConnectionError("connection reset"))
    items = [
        _nitf_item("acq-a", products["acq-a"]),
        _nitf_item("acq-flaky", url),
    ]
    prepared: list[str] = []

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, prepared),
        preflight=True,
    )

    assert prepared == ["acq-a", "acq-flaky"]
    assert dataset.skipped == ()
    assert dataset.preflight is not None
    assert dataset.preflight.unreadable == 1
    assert dataset.preflight.missing == 0
    assert dataset.preflight.supported == 1


def test_an_acquisition_with_no_readable_product_is_dropped_like_a_refusal(tmp_path):
    """The failure a preflight *can* decide, and the one keeping was worst for.

    Nothing is at this acquisition's href. That is as final as any refusal, and
    keeping the pass was never the cautious choice it resembled: `chip_item`
    would have raised a plain read error, which `skip_unsupported` does not catch
    by design, so the run would have ended on a pass its own preflight had
    already ruled out.
    """
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    items = [
        _nitf_item("acq-a", products["acq-a"]),
        _nitf_item("acq-gone", tmp_path / "does-not-exist.nitf"),
    ]
    prepared: list[str] = []

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, prepared),
        preflight=True,
        skip_unsupported=True,
    )

    assert prepared == ["acq-a"]
    # Recorded as the hole it is, in the reader's own words, at the stage it was
    # found -- the same object a survived refusal produces.
    assert [s.item_id for s in dataset.skipped] == ["acq-gone"]
    assert dataset.skipped[0].stage == "preflight"
    assert "does not exist" in dataset.skipped[0].reason
    assert dataset.preflight is not None
    assert dataset.preflight.missing == 1
    assert dataset.preflight.unreadable == 0
    assert dataset.preflight.skipped == 1
    assert dataset.preflight.supported == 1


def test_keeping_an_unreadable_pass_is_what_would_have_ended_the_run(tmp_path):
    """The reason the drop above is right, stated as the failure it avoids.

    `skip_unsupported` catches `UnsupportedMeasurementError` and nothing else, on
    purpose -- a batch that swallows unknown errors is one nobody can trust. So a
    pass with no product behind it is fatal at conversion time even to a run that
    asked to survive refusals, which is exactly the run a preflight is for.
    """
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    products = _write_products(tmp_path)
    items = [_nitf_item("acq-gone", tmp_path / "does-not-exist.nitf")]

    def _prepare(item, asset, work_dir, conversion):
        raise FileNotFoundError(item.asset_href("SICD"))

    with pytest.raises(FileNotFoundError):
        write_chips(
            items,
            tmp_path / "ds",
            asset="SICD",
            chip_size=10,
            conversion=SicdConversion(calibration="sigma0"),
            preparer=_prepare,
            skip_unsupported=True,
        )

    # Same selection, same settings, with the check in front of it: the run
    # completes and says what it left out.
    dataset = write_chips(
        [*items, _nitf_item("acq-a", products["acq-a"])],
        tmp_path / "ds2",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(),
        preparer=lambda item, asset, work_dir, conversion: cog,
        preflight=True,
        skip_unsupported=True,
    )
    assert [s.item_id for s in dataset.skipped] == ["acq-gone"]


def test_a_preflight_asks_the_settings_the_conversion_will_use(tmp_path):
    """A pass the preflight clears cannot then be refused for a reason it saw."""
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    products = _write_products(tmp_path)
    items = [_nitf_item(name, path) for name, path in products.items()]

    # No calibration asked for, so the uncalibrated product answers fine and
    # nothing is dropped: the question follows the request rather than a default.
    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(),
        preparer=_recording_preparer(cog, []),
        preflight=True,
    )

    assert dataset.skipped == ()
    assert dataset.preflight is not None
    assert dataset.preflight.supported == 3


def test_the_batchs_preflight_reads_the_headers_concurrently(tmp_path, monkeypatch):
    """The check runs *in front of* the batch, so a serial walk over a large site
    is a stall that grows with the number of passes. Every read waits for all
    three here: a one-at-a-time preflight cannot clear that barrier."""
    import threading

    pytest.importorskip("numpy")
    from umbra_py import preflight as preflight_mod
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    items = [_nitf_item(name, path) for name, path in products.items()]

    gate = threading.Barrier(len(items), timeout=30)
    real = preflight_mod.sicd_capabilities

    def gated(src, **kwargs):
        gate.wait()
        return real(src, **kwargs)

    monkeypatch.setattr(preflight_mod, "sicd_capabilities", gated)
    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, []),
        preflight=True,
        preflight_workers=len(items),
    )

    assert dataset.preflight is not None
    # Concurrency is a schedule, not an answer: the same pass is dropped, for the
    # same reason, as the serial walk above found.
    assert [s.item_id for s in dataset.skipped] == ["acq-b"]
    assert dataset.preflight.checked == len(items)


def test_preflight_is_refused_on_an_asset_that_carries_no_metadata_to_ask(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", nodata_corner=False)
    with pytest.raises(ValueError, match="complex asset"):
        write_chips(
            [_item_for(tif)],
            tmp_path / "ds",
            asset="GEC",
            chip_size=10,
            preflight=True,
        )


def test_the_preflight_block_is_absent_from_a_run_that_did_not_ask(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    dataset = write_chips(
        [_sicd_item("acq-a", cog)],
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        preparer=_refusing_preparer(cog, refuse=set()),
    )

    assert dataset.preflight is None
    assert "preflight" not in dataset.to_dict()


def test_the_preflight_roll_up_reaches_the_json_summary(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import SicdConversion, write_chips

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    items = [_nitf_item(name, path) for name, path in products.items()]

    dataset = write_chips(
        items,
        tmp_path / "ds",
        asset="SICD",
        chip_size=10,
        conversion=SicdConversion(calibration="sigma0"),
        preparer=_recording_preparer(cog, []),
        preflight=True,
    )

    payload = dataset.to_dict()
    assert payload["preflight"]["checked"] == 3
    assert payload["preflight"]["skipped"] == 1
    assert payload["preflight"]["product_bytes_skipped"] > 0
    # The dropped pass is in the same `skipped` block a survived refusal uses,
    # so a consumer needs no second vocabulary to find the dataset's holes.
    assert payload["skipped_count"] == 1
    assert payload["skipped"][0]["stage"] == "preflight"


def test_cli_chips_preflight_says_what_it_dropped_and_what_that_saved(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(tmp_path / "geocoded.tif", calibration="sigma0")
    products = _write_products(tmp_path)
    hrefs = {
        "http://example.com/a.json": ("acq-a", products["acq-a"]),
        "http://example.com/b.json": ("acq-b", products["acq-b"]),
    }

    def _get_json(url):
        item_id, path = hrefs[url]
        return {
            "id": item_id,
            "properties": {"datetime": "2024-02-08T12:00:00Z", "sar:product_type": "SICD"},
            "assets": {"SICD": {"href": str(path)}},
        }

    monkeypatch.setattr("umbra_py.cli._shared.get_json", _get_json)
    real_write_chips = chips_mod.write_chips

    def _spy(items, out_dir, **kwargs):
        return real_write_chips(items, out_dir, preparer=_recording_preparer(cog, []), **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            *hrefs,
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--chip-size",
            "10",
            "--calibrate",
            "sigma0",
            "--preflight",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Preflight read" in result.output
    assert "dropped 1" in result.output
    assert "acq-b [preflight]" in result.output
    # Nothing was missing here, so the console says nothing about it: the line
    # exists to distinguish two kinds of hole, not to report the absence of one.
    assert "no readable product" not in result.output


def test_cli_chips_preflight_names_the_passes_it_had_no_product_for(tmp_path, monkeypatch):
    """`--calibrate` was not even asked for here: the drop is not a refusal but
    the absence of anything to refuse, and the report keeps the two apart."""
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import chips as chips_mod
    from umbra_py import cli as cli_mod

    cog = _make_converted_cog(tmp_path / "geocoded.tif")
    products = _write_products(tmp_path)
    hrefs = {
        "http://example.com/a.json": ("acq-a", products["acq-a"]),
        "http://example.com/gone.json": ("acq-gone", tmp_path / "never-written.nitf"),
    }

    def _get_json(url):
        item_id, path = hrefs[url]
        return {
            "id": item_id,
            "properties": {"datetime": "2024-02-08T12:00:00Z", "sar:product_type": "SICD"},
            "assets": {"SICD": {"href": str(path)}},
        }

    monkeypatch.setattr("umbra_py.cli._shared.get_json", _get_json)
    real_write_chips = chips_mod.write_chips

    def _spy(items, out_dir, **kwargs):
        return real_write_chips(items, out_dir, preparer=_recording_preparer(cog, []), **kwargs)

    monkeypatch.setattr(chips_mod, "write_chips", _spy)

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            *hrefs,
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--chip-size",
            "10",
            "--preflight",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 of those had no readable product to ask" in result.output
    assert "acq-gone [preflight]" in result.output


def test_cli_chips_preflight_is_refused_on_a_raster_asset(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "GEC",
            "--preflight",
        ],
    )

    assert result.exit_code != 0
    assert "--preflight applies to the complex products" in result.output


def test_cli_chips_refuses_a_preflight_lane_count_below_one(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(tmp_path / "ds"),
            "--asset",
            "SICD",
            "--preflight",
            "--preflight-workers",
            "0",
        ],
    )

    assert result.exit_code != 0
    assert "--preflight-workers" in result.output
