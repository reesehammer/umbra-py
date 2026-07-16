"""Offline tests for auto-fetching a Copernicus DEM (``umbra_py.dem``).

The tile math (id naming, bbox coverage, URL building) is pure standard library
and tested with no network. The fetch is exercised through an *injected*
``download`` callable -- one that writes a stub file, and one that raises a
"tile does not exist" (404) for the ocean gaps Copernicus omits -- so the
skip/merge/raise behaviour is covered without touching the bucket. The multi-tile
mosaic path additionally builds real one-degree DEM rasters and merges them with
``rasterio`` (the ``[convert]`` extra).
"""

from __future__ import annotations

import pytest

from umbra_py import dem

# --------------------------------------------------------------------------- #
# Pure tile math (no optional extras).
# --------------------------------------------------------------------------- #


def test_copernicus_tile_id_hemispheres_and_padding():
    assert dem.copernicus_tile_id(45, 6) == "Copernicus_DSM_COG_10_N45_00_E006_00_DEM"
    assert dem.copernicus_tile_id(-1, -1) == "Copernicus_DSM_COG_10_S01_00_W001_00_DEM"
    assert dem.copernicus_tile_id(0, 0) == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"
    # Longitude is three digits, latitude two.
    assert dem.copernicus_tile_id(9, 123) == "Copernicus_DSM_COG_10_N09_00_E123_00_DEM"


def test_tile_url_builds_public_bucket_path():
    tid = "Copernicus_DSM_COG_10_N45_00_E006_00_DEM"
    assert dem.tile_url(tid) == f"{dem.COPERNICUS_DEM_30M_BASE}/{tid}/{tid}.tif"
    assert dem.tile_url(tid, base="https://x/") == f"https://x/{tid}/{tid}.tif"


def test_tiles_covering_bbox_single_cell():
    # A small scene well inside one degree cell -> exactly one tile.
    assert dem.tiles_covering_bbox(6.2, 45.2, 6.4, 45.4) == [(45, 6)]


def test_tiles_covering_bbox_spans_cells_and_is_ordered():
    tiles = dem.tiles_covering_bbox(5.9, 44.9, 6.1, 45.1)
    # Straddles the 45/6 degree lines -> a 2x2 block, south-to-north west-to-east.
    assert tiles == [(44, 5), (44, 6), (45, 5), (45, 6)]


def test_tiles_covering_bbox_negative_and_floor():
    # A point at (-0.5, -0.5) floors into the S01/W001 cell.
    assert dem.tiles_covering_bbox(-0.5, -0.5, -0.4, -0.4) == [(-1, -1)]


def test_tiles_covering_bbox_clamps_latitude():
    # Near the pole, latitude is clamped to Copernicus' valid [-90, 89].
    tiles = dem.tiles_covering_bbox(10.2, 88.5, 10.4, 91.0)
    assert all(-90 <= lat <= 89 for lat, _ in tiles)
    assert (89, 10) in tiles


def test_tiles_covering_bbox_rejects_degenerate():
    with pytest.raises(ValueError, match="degenerate"):
        dem.tiles_covering_bbox(6.0, 45.0, 5.0, 45.0)


def test_tile_ids_for_bbox_matches_ids():
    ids = dem.tile_ids_for_bbox(6.2, 45.2, 6.4, 45.4)
    assert ids == ["Copernicus_DSM_COG_10_N45_00_E006_00_DEM"]


def test_default_dem_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("UMBRA_DEM_DIR", str(tmp_path / "d"))
    assert dem.default_dem_cache_dir() == tmp_path / "d"
    monkeypatch.delenv("UMBRA_DEM_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert dem.default_dem_cache_dir() == tmp_path / "cache" / "umbra-py" / "dem"


# --------------------------------------------------------------------------- #
# Fetch behaviour with an injected downloader (no network).
# --------------------------------------------------------------------------- #


class _NotFound(Exception):
    """Stand-in for the HTTP error a missing (all-ocean) Copernicus tile raises."""

    def __init__(self):
        super().__init__("404 Client Error: Not Found")
        self.response = type("Resp", (), {"status_code": 404})()


def _tid_from_url(url: str) -> str:
    return url.rsplit("/", 2)[1]


def test_fetch_dem_single_tile_returns_the_tile(tmp_path):
    calls = []

    def fake_download(url, dest, *, session=None):
        calls.append(url)
        from pathlib import Path

        dest = Path(dest)
        dest.write_bytes(b"stub-dem")
        return dest

    out = dem.fetch_dem_for_bbox((6.2, 45.2, 6.4, 45.4), tmp_path, download=fake_download)
    assert out == tmp_path / "Copernicus_DSM_COG_10_N45_00_E006_00_DEM.tif"
    assert out.read_bytes() == b"stub-dem"
    assert len(calls) == 1  # exactly one tile fetched


def test_fetch_dem_skips_missing_ocean_tiles(tmp_path):
    present = {"Copernicus_DSM_COG_10_N45_00_E006_00_DEM"}

    def fake_download(url, dest, *, session=None):
        from pathlib import Path

        if _tid_from_url(url) not in present:
            raise _NotFound()  # ocean gap
        dest = Path(dest)
        dest.write_bytes(b"stub-dem")
        return dest

    # A 2x2 block where only one cell has a tile -> the single present tile.
    out = dem.fetch_dem_for_bbox((5.9, 44.9, 6.1, 45.1), tmp_path, download=fake_download)
    assert out.name == "Copernicus_DSM_COG_10_N45_00_E006_00_DEM.tif"


def test_fetch_dem_all_ocean_raises(tmp_path):
    def fake_download(url, dest, *, session=None):
        raise _NotFound()

    with pytest.raises(dem.DemUnavailableError, match="all ocean"):
        dem.fetch_dem_for_bbox((6.2, 45.2, 6.4, 45.4), tmp_path, download=fake_download)


def test_fetch_dem_propagates_non_404_errors(tmp_path):
    def fake_download(url, dest, *, session=None):
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError, match="connection reset"):
        dem.fetch_dem_for_bbox((6.2, 45.2, 6.4, 45.4), tmp_path, download=fake_download)


# --------------------------------------------------------------------------- #
# Multi-tile mosaic (rasterio, the [convert] extra).
# --------------------------------------------------------------------------- #


def _write_degree_dem(path, lat_deg, lon_deg, value):
    """A valid one-degree EPSG:4326 DEM raster for the (lat, lon) SW corner."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    h = w = 8
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_bounds(lon_deg, lat_deg, lon_deg + 1, lat_deg + 1, w, h),
    }
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(np.full((h, w), float(value), dtype="float32"), 1)
    return path


def test_fetch_dem_merges_adjacent_tiles(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("numpy")

    def fake_download(url, dest, *, session=None):
        tid = _tid_from_url(url)
        # E006 -> value 6, E007 -> value 7; both in the N45 row.
        lon = 6 if "E006" in tid else 7
        return _write_degree_dem(dest, 45, lon, float(lon))

    # A bbox straddling 6/7 longitude within the 45 latitude cell -> two tiles.
    out = dem.fetch_dem_for_bbox((6.5, 45.2, 7.5, 45.8), tmp_path, download=fake_download)
    assert out.name.startswith("dem_mosaic_")
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326
        # The mosaic spans both degree cells: ~2 degrees wide.
        assert ds.bounds.right - ds.bounds.left == pytest.approx(2.0, abs=0.3)

    # A repeat call returns the same cached mosaic path (deterministic name).
    out2 = dem.fetch_dem_for_bbox((6.5, 45.2, 7.5, 45.8), tmp_path, download=fake_download)
    assert out2 == out
