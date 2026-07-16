"""Auto-fetch a global geoid-undulation grid for ``umbra convert --geoid auto``.

``umbra convert --geoid PATH`` corrects the DEM heights terrain
orthorectification samples from *orthometric* (height above the geoid, which
global DEMs like Copernicus GLO-30 and SRTM quote) to *ellipsoidal* (HAE, which
SICD's projection wants) by adding the geoid undulation ``N`` at each point.
Finding, downloading and pointing at the right EGM grid for that is exactly the
"same 500 lines of glue" the project exists to remove, so ``--geoid auto`` /
:func:`umbra_py.convert.sicd_to_geocoded_cog` (``geoid="auto"``) does it for you:
fetch a global geoid-undulation grid and hand back a single rasterio-openable
raster whose band 1 is ``N`` in metres.

Design, in the project's grain — the vertical sibling of :mod:`umbra_py.dem`:

- **The grid is a public, host-anywhere artifact.** The default is the
  `EGM96 15′ model <https://cdn.proj.org/us_nga_egm96_15.tif>`_ PROJ distributes
  on its CDN for datum transformations — a compact (~4 MB) *global* GeoTIFF, so
  unlike a DEM there is nothing to tile: one file covers every scene. Its band-1
  value is the geoid undulation ``N`` (ellipsoid − geoid separation) in metres,
  exactly what :func:`umbra_py.convert._geoid_corrected_sampler` adds — so the
  fetched grid feeds straight into the shipped ``--geoid PATH`` path unchanged.
- **The fetch reuses the resume-safe :func:`~umbra_py.download.download_url`**
  and is *injectable* (the ``download`` argument), so :func:`fetch_geoid_grid`
  is exercised without hitting the CDN, mirroring the discipline in
  :mod:`umbra_py.dem` and :mod:`umbra_py.convert`.
- **The grid is cached** under the same XDG cache dir the catalog index and the
  auto-fetched DEM tiles use, so a second conversion re-downloads nothing.

``us_nga_egm08_25.tif`` (EGM2008, 2.5′) is a higher-resolution alternative on
the same CDN; pass it as ``name=`` if the extra precision is worth the larger
download. Either way the undulation error against the raw ellipsoid–geoid
separation the correction removes (~±100 m worldwide) is sub-metre.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .download import download_url

#: Public HTTPS endpoint of the PROJ CDN, which hosts the EGM geoid grids PROJ
#: uses for vertical datum transformations. Each grid is a single global file.
PROJ_CDN_BASE = "https://cdn.proj.org"

#: Default geoid grid: EGM96 at 15′, a compact global GeoTIFF whose band 1 is the
#: geoid undulation ``N`` (ellipsoid − geoid separation) in metres.
DEFAULT_GEOID_GRID = "us_nga_egm96_15.tif"

#: Sentinel value accepted by ``geoid=`` / ``--geoid`` to request an auto-fetch.
AUTO = "auto"

#: Type of an injectable downloader matching :func:`umbra_py.download.download_url`.
Downloader = Callable[..., Path]


def default_geoid_cache_dir() -> Path:
    """Where the auto-fetched geoid grid is cached.

    ``$UMBRA_GEOID_DIR`` overrides everything; otherwise the grid sits beside the
    catalog index and DEM tiles under the XDG cache dir (``$XDG_CACHE_HOME`` or
    ``~/.cache``) at ``umbra-py/geoid``.
    """
    override = os.environ.get("UMBRA_GEOID_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "umbra-py" / "geoid"


def geoid_grid_url(name: str = DEFAULT_GEOID_GRID, *, base: str = PROJ_CDN_BASE) -> str:
    """Public HTTPS URL of the geoid grid ``name`` on the PROJ CDN."""
    return f"{base.rstrip('/')}/{name}"


def fetch_geoid_grid(
    dest_dir: str | os.PathLike | None = None,
    *,
    name: str = DEFAULT_GEOID_GRID,
    base: str = PROJ_CDN_BASE,
    download: Downloader = download_url,
    session=None,
) -> Path:
    """Fetch a global geoid-undulation grid and return its local path.

    The grid ``name`` (default :data:`DEFAULT_GEOID_GRID`, the EGM96 15′ model) is
    downloaded once to ``dest_dir`` (default :func:`default_geoid_cache_dir`) via
    the resume-safe ``download`` callable and cached there, so a repeat call
    re-downloads nothing. The return is a single rasterio-openable raster whose
    band 1 is the geoid undulation ``N`` in metres — exactly what
    :func:`umbra_py.convert._geoid_corrected_sampler` adds to each sampled DEM
    height, so it feeds straight into the ``--geoid PATH`` path.

    Unlike a DEM there is nothing to tile: the EGM grid is global, so one file
    covers every scene and no footprint bbox is needed. ``download`` is injectable
    so the whole path is offline-testable without the CDN.
    """
    dest = Path(dest_dir) if dest_dir is not None else default_geoid_cache_dir()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    return Path(download(geoid_grid_url(name, base=base), target, session=session))
