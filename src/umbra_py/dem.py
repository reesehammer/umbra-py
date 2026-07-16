"""Auto-fetch a Copernicus GLO-30 DEM covering a scene footprint.

``umbra convert --dem PATH`` terrain-orthorectifies a SICD against a digital
elevation model the user supplies. Finding, downloading and mosaicking the right
tiles for a scene is exactly the "same 500 lines of glue" the project exists to
remove, so ``--dem auto`` / :func:`umbra_py.convert.sicd_to_geocoded_cog`
(``dem="auto"``) does it for you: resolve the 1°×1° `Copernicus GLO-30
<https://registry.opendata.aws/copernicus-dem/>`_ DEM tiles that cover the
scene's geographic bounding box, pull them from the public AWS Open Data bucket
(skipping the ocean gaps that 404), and hand back a single rasterio-openable
DEM — one tile as-is, several merged into a mosaic.

Design, in the project's grain:

- **The tile math is pure standard library** (`copernicus_tile_id`,
  `tiles_covering_bbox`, `tile_url`) — a Copernicus tile is named by its
  south-west integer-degree corner, so covering a bbox is a integer floor/range,
  offline-tested with no network.
- **The fetch reuses the resume-safe :func:`~umbra_py.download.download_url`**
  and is *injectable* (the ``download`` argument), so :func:`fetch_dem_for_bbox`
  is exercised without hitting the bucket. Only the multi-tile *mosaic* step
  touches ``rasterio`` (the ``[convert]`` extra), mirroring the discipline in
  :mod:`umbra_py.convert`.
- **Tiles are cached** under the same XDG cache dir the catalog index uses, so a
  second conversion over the same area re-downloads nothing.

Assumptions worth stating: GLO-30 covers −90..90 latitude and does not cross the
antimeridian in a single tile; a SICD scene is a few kilometres across, so its
footprint never straddles the ±180° seam — the resolver does not handle that
wrap and a scene there should pass an explicit ``--dem PATH`` instead.
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Callable
from pathlib import Path

from .download import download_url
from .exceptions import UmbraError

#: Public HTTPS endpoint of the Copernicus GLO-30 (30 m) DEM AWS Open Data
#: bucket. Each tile lives at ``<base>/<tile_id>/<tile_id>.tif``.
COPERNICUS_DEM_30M_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"

#: Sentinel value accepted by ``dem=`` / ``--dem`` to request an auto-fetch.
AUTO = "auto"

#: Type of an injectable downloader matching :func:`umbra_py.download.download_url`.
Downloader = Callable[..., Path]


class DemUnavailableError(UmbraError):
    """No Copernicus DEM tile covers the requested footprint (e.g. all ocean)."""


def default_dem_cache_dir() -> Path:
    """Where auto-fetched DEM tiles are cached.

    ``$UMBRA_DEM_DIR`` overrides everything; otherwise tiles sit beside the
    catalog index under the XDG cache dir (``$XDG_CACHE_HOME`` or ``~/.cache``)
    at ``umbra-py/dem``.
    """
    override = os.environ.get("UMBRA_DEM_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "umbra-py" / "dem"


def copernicus_tile_id(lat_deg: int, lon_deg: int) -> str:
    """Copernicus GLO-30 tile id for the 1°×1° cell whose SW corner is ``(lat, lon)``.

    Copernicus tiles are named by their south-west *integer-degree* corner, so
    the cell containing a point is found by flooring its coordinates. ``lat_deg``
    / ``lon_deg`` are those floored degrees (``lat_deg`` in ``[-90, 89]``).

    >>> copernicus_tile_id(45, 6)
    'Copernicus_DSM_COG_10_N45_00_E006_00_DEM'
    >>> copernicus_tile_id(-1, -1)
    'Copernicus_DSM_COG_10_S01_00_W001_00_DEM'
    """
    ns = "N" if lat_deg >= 0 else "S"
    ew = "E" if lon_deg >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_deg):02d}_00_{ew}{abs(lon_deg):03d}_00_DEM"


def tile_url(tile_id: str, *, base: str = COPERNICUS_DEM_30M_BASE) -> str:
    """Public HTTPS URL of a Copernicus DEM ``tile_id`` on the open-data bucket."""
    return f"{base.rstrip('/')}/{tile_id}/{tile_id}.tif"


def tiles_covering_bbox(
    west: float, south: float, east: float, north: float
) -> list[tuple[int, int]]:
    """SW-corner ``(lat_deg, lon_deg)`` of every 1°×1° tile covering the bbox.

    The tiles are the integer-degree cells from ``floor(south)..floor(north)`` in
    latitude and ``floor(west)..floor(east)`` in longitude, inclusive — so a bbox
    landing exactly on a degree line still pulls the neighbouring cell (extra
    coverage never hurts a DEM lookup). Latitude is clamped to Copernicus' valid
    ``[-90, 89]`` range; ordering is deterministic (south-to-north, west-to-east).
    """
    if east < west or north < south:
        raise ValueError(f"degenerate bbox: {(west, south, east, north)}")
    lat_lo = max(-90, math.floor(south))
    lat_hi = min(89, math.floor(north))
    lon_lo = math.floor(west)
    lon_hi = math.floor(east)
    return [(lat, lon) for lat in range(lat_lo, lat_hi + 1) for lon in range(lon_lo, lon_hi + 1)]


def tile_ids_for_bbox(west: float, south: float, east: float, north: float) -> list[str]:
    """Tile ids of every Copernicus DEM cell covering the bbox.

    See :func:`tiles_covering_bbox` for the coverage rule.
    """
    cells = tiles_covering_bbox(west, south, east, north)
    return [copernicus_tile_id(lat, lon) for lat, lon in cells]


def _is_missing_tile(exc: Exception) -> bool:
    """Whether ``exc`` is a "tile does not exist" signal (ocean gap), not a real failure.

    Copernicus omits tiles that are entirely ocean, so a 404 (or a
    :class:`~umbra_py.exceptions.DownloadError` wrapping one) for a covering tile
    is expected and skipped; any other error propagates.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (403, 404):
        return True
    text = str(exc)
    return "404" in text or "403" in text


def fetch_dem_for_bbox(
    bbox: tuple[float, float, float, float],
    dest_dir: str | os.PathLike | None = None,
    *,
    base: str = COPERNICUS_DEM_30M_BASE,
    download: Downloader = download_url,
    session=None,
) -> Path:
    """Fetch the Copernicus GLO-30 DEM covering ``bbox`` and return a local raster path.

    ``bbox`` is ``(west, south, east, north)`` in EPSG:4326 degrees. Every 1°×1°
    tile covering it is downloaded to ``dest_dir`` (default
    :func:`default_dem_cache_dir`) via the resume-safe ``download`` callable;
    tiles Copernicus omits because they are all-ocean (a 404) are skipped. The
    return is a single rasterio-openable DEM: the one tile as-is when the bbox
    falls in a single cell, or a merged mosaic (``dem_mosaic_*.tif`` in
    ``dest_dir``) when it spans several.

    Raises :class:`DemUnavailableError` when no covering tile exists (e.g. a
    scene entirely over open water — pass an explicit ``--dem PATH`` there).

    ``download`` is injectable so the whole path is offline-testable without the
    bucket; only the multi-tile mosaic step imports ``rasterio``.
    """
    west, south, east, north = bbox
    dest = Path(dest_dir) if dest_dir is not None else default_dem_cache_dir()
    dest.mkdir(parents=True, exist_ok=True)

    ids = tile_ids_for_bbox(west, south, east, north)
    fetched: list[Path] = []
    for tid in ids:
        target = dest / f"{tid}.tif"
        try:
            path = download(tile_url(tid, base=base), target, session=session)
        except Exception as exc:  # noqa: BLE001 -- a missing tile is expected; re-raise anything else
            if _is_missing_tile(exc):
                continue
            raise
        fetched.append(Path(path))

    if not fetched:
        raise DemUnavailableError(
            f"no Copernicus GLO-30 DEM tile covers bbox {bbox} "
            "(all ocean?); pass an explicit --dem PATH instead"
        )
    if len(fetched) == 1:
        return fetched[0]
    return _merge_tiles(fetched, dest)


def _merge_tiles(paths: list[Path], dest_dir: Path) -> Path:
    """Mosaic several DEM tiles into one GeoTIFF (rasterio-only; ``[convert]`` extra).

    The output name is derived from the sorted tile stems so a repeat call over
    the same tiles returns the same cached mosaic path.
    """
    import rasterio  # noqa: PLC0415
    from rasterio.merge import merge  # noqa: PLC0415

    key = "_".join(sorted(p.stem for p in paths))
    digest = hashlib.sha1(key.encode()).hexdigest()[:8]
    out = dest_dir / f"dem_mosaic_{digest}.tif"
    if out.exists():
        return out

    datasets = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(datasets)
        profile = datasets[0].profile.copy()
        profile.update(
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            transform=transform,
            count=mosaic.shape[0],
        )
        with rasterio.open(out, "w", **profile) as ds:
            ds.write(mosaic)
    finally:
        for ds in datasets:
            ds.close()
    return out
