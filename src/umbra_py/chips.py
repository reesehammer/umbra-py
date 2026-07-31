"""umbra chips: turn SAR scenes into georeferenced ML training tiles.

For the model-*training* audience, the missing verb is *chipping*: walking a
search result and cutting each scene into fixed-size, georeferenced tiles with a
manifest that carries the metadata a training pipeline needs (look angle,
resolution, polarization, license). This is the data-loading layer for SAR
foundation-model and change-detection research (the C4 workstream,
``docs/STRATEGY.md`` 5.5) -- the audience most likely to contribute back and
the one that turns free Umbra pixels into demand for Umbra pixels.

Design, following the package's determinism boundary (``docs/AGENTS.md``):

- **No model is called.** Chipping is pure raster iteration + manifest logic;
  it stays in the deterministic core behind the ``[load]`` extra (``rasterio`` +
  ``numpy``), exactly like :mod:`umbra_py.load`, which it mirrors. For an
  amplitude product it reads band 1 of the item's geocoded GeoTIFF through
  GDAL's ``/vsicurl/`` driver, so only the bytes for each tile are streamed over
  HTTP range requests -- no multi-gigabyte download, and memory stays bounded to
  one chip at a time.
- **Fixed-size is a promise.** Only full ``chip_size`` x ``chip_size`` tiles are
  emitted; partial edge tiles are dropped, so every chip a training loader sees
  has the shape it expects. ``stride`` controls overlap (``stride < chip_size``
  produces overlapping tiles for dense inference / augmentation).
- **Empty tiles are filtered, not shipped.** A geocoded SAR scene is a rotated
  footprint inside a north-up raster, so its corners are nodata. ``min_valid``
  drops tiles whose valid (finite, positive) fraction falls below a threshold,
  so a dataset isn't padded with black squares.
- **Every chip carries its provenance.** Each manifest record has the chip's
  geographic bbox, CRS, affine transform, and the acquisition metadata a model
  needs, plus the mandatory CC-BY attribution -- the same license discipline the
  library applies to GeoTIFF tags and xarray attrs, extended to the manifest.
- **The complex products are chipped through the conversion pipeline.** A
  ``SICD`` is not a display raster -- it is complex samples in the slant plane,
  with no map grid to cut a georeferenced tile out of -- so for a long time the
  chipper simply refused it, and the full-resolution half of Umbra's archive
  (the half that is the point of 16-25 cm SAR) stayed out of reach of a training
  loader. It is reachable now because :mod:`umbra_py.convert` shipped: with
  ``asset="SICD"`` the acquisition is fetched, geocoded to a north-up
  EPSG:4326 COG (optionally terrain-orthorectified, terrain-flattened and
  radiometrically calibrated -- see :class:`SicdConversion`), and then chipped by
  the *same* window loop that reads a GEC. Nothing about a chip's shape,
  manifest or provenance changes; only where its pixels came from. The cost is
  honest and stated: unlike the GEC path this downloads the whole product before
  it can read any of it, so it is opt-in, one scene is resident at a time, and
  ``work_dir`` makes the expensive step resumable across runs.

The manifest is machine-readable first: ``.jsonl`` (one chip record per line --
the standard ML manifest format) or ``.geojson`` (a ``FeatureCollection`` of
chip footprints for QGIS / geopandas), both stdlib-only. A third format,
``.parquet``, writes the same chip footprints as `stac-geoparquet
<https://stac-geoparquet.org/>`__ -- one column-oriented file DuckDB, geopandas
or pyarrow can query without loading every line, the format a *large* chip set
wants (the same plumbing :mod:`umbra_py.export` uses for the catalog snapshot).
It needs the ``[export]`` extra alongside ``[load]``.

Install with: ``pip install "umbra-py[load]"`` (add ``[export]`` for
``.parquet`` manifests).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .constants import ATTRIBUTION, DATA_LICENSE
from .exceptions import AssetNotFoundError
from .load import _open_path, _require
from .models import UmbraItem

#: Product types that are already amplitude rasters, read directly over HTTP
#: range requests with no preparation.
RASTER_ASSETS = ("GEC", "CSI")

#: Product types that are complex slant-plane data, chipped by geocoding them
#: first (see :class:`SicdConversion`). ``CPHD`` is phase history rather than a
#: focused image, so it has no image grid to chip and stays out.
COMPLEX_ASSETS = ("SICD",)

#: Product types this chipper can read. ``SIDD`` is a NITF that GDAL can read
#: but is out of scope.
CHIPPABLE_ASSETS = (*RASTER_ASSETS, *COMPLEX_ASSETS)

#: Progress callback: ``(item_index, item_total, item, chips_written)``.
ProgressFn = Callable[[int, int, UmbraItem, int], None]


def _safe_slug(text: str) -> str:
    """A filesystem-safe slug for a chip filename stem (keeps names collision-free
    across items while staying readable)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return slug or "item"


@dataclass(frozen=True)
class SicdConversion:
    """How a complex ``SICD`` is geocoded before it is chipped.

    Every field is passed straight to :func:`umbra_py.convert.sicd_to_geocoded_cog`
    and means exactly what it means there -- this is the chipper's handle on that
    pipeline, not a second implementation of it. The defaults are the flat-earth
    geocoding, which is what a training set wants unless the site has relief.

    The one option deliberately *not* exposed is ``decibels``: the chipper's own
    ``db`` flag already chooses the scale, so the conversion always writes linear
    amplitude and the chip loop takes the logarithm. That keeps one code path for
    both asset kinds, and keeps a calibrated chip's decibels the decibels *of the
    calibrated quantity*.

    ``bbox`` is set from :func:`chip_item`'s own ``bbox`` rather than passed
    separately: chipping an area of interest out of a complex product means
    geocoding only that area, so the two are one decision. It is part of
    :meth:`cache_key` like every other field, so a clipped conversion never
    stands in for a whole-scene one in ``work_dir``.
    """

    dem: str | None = None
    geoid: str | None = None
    rtc: bool = False
    rtc_model: str = "cosine"
    rtc_reference_deg: float | None = None
    calibration: str | None = None
    resolution: float | None = None
    resampling: str = "bilinear"
    gcp_grid: int = 15
    projection_type: str = "HAE"
    bbox: tuple[float, float, float, float] | None = None

    def cache_key(self) -> str:
        """A short stable digest of these settings.

        Two conversions of one acquisition differ only by these values, so the
        digest is what makes a cached geocoded COG in ``work_dir`` safe to reuse:
        change a setting and the name changes with it, rather than silently
        chipping the previous product.
        """
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


#: Prepares a complex product for chipping: ``(item, asset, work_dir, conversion)``
#: -> the path of a geocoded amplitude raster. Injectable so the whole chipping
#: path is testable without ``sarpy``, a network, or a multi-gigabyte NITF.
SicdPreparer = Callable[[UmbraItem, str, Path, SicdConversion], Path]


def _provenance_tags(dataset: Any) -> dict[str, str]:
    """The ``UMBRA_*`` conversion tags an open raster carries (empty if none).

    A GEC streamed from the bucket has none -- it is the published product, not
    something this library made -- so the absence is the honest answer, not a
    gap to fill in.
    """
    from .convert import PROVENANCE_TAG_PREFIX  # noqa: PLC0415

    return {
        key: value for key, value in dataset.tags().items() if key.startswith(PROVENANCE_TAG_PREFIX)
    }


def _reported_step(provenance: dict[str, str], name: str) -> str | None:
    """One processing step from the provenance tags, as ``None`` when it did not run.

    :func:`umbra_py.convert.conversion_tags` writes ``"none"`` rather than
    omitting a step that was skipped, so that a tag's absence never has to be
    interpreted; a manifest field is the other convention, so translate once
    here instead of at each use.
    """
    from .convert import PROVENANCE_TAG_PREFIX  # noqa: PLC0415

    value = provenance.get(f"{PROVENANCE_TAG_PREFIX}{name}")
    return None if value in (None, "none") else value


def _as_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """A bbox as four plain floats, so one written any sequence-ish way compares
    and digests identically wherever it is stored."""
    west, south, east, north = (float(v) for v in bbox)
    return west, south, east, north


def _clip_pixel_window(
    src: Any, bbox: tuple[float, float, float, float]
) -> tuple[int, int, int, int]:
    """The raster window ``(row0, col0, row_stop, col_stop)`` covering a lon/lat bbox.

    Rounded *outward* to whole pixels and clamped to the raster, so the tiling
    loop can never run off the edge and the requested area is never trimmed by
    rounding. The bbox is lon/lat whatever the raster's CRS is — the same
    convention :func:`umbra_py.to_stack`'s ``bbox=`` follows.
    """
    from rasterio.warp import transform_bounds  # noqa: PLC0415
    from rasterio.windows import from_bounds  # noqa: PLC0415

    west, south, east, north = (float(v) for v in bbox)
    if east <= west or north <= south:
        raise ValueError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat) with a positive "
            f"extent, got {tuple(bbox)!r}."
        )
    if src.crs is not None:
        west, south, east, north = transform_bounds("EPSG:4326", src.crs, west, south, east, north)
    window = from_bounds(west, south, east, north, transform=src.transform)
    row0 = max(0, int(math.floor(window.row_off)))
    col0 = max(0, int(math.floor(window.col_off)))
    row_stop = min(src.height, int(math.ceil(window.row_off + window.height)))
    col_stop = min(src.width, int(math.ceil(window.col_off + window.width)))
    if row_stop <= row0 or col_stop <= col0:
        raise ValueError(
            f"bbox {tuple(bbox)!r} does not overlap the raster, so there is nothing to chip."
        )
    return row0, col0, row_stop, col_stop


@contextlib.contextmanager
def _chip_source(
    item: UmbraItem,
    asset: str,
    *,
    conversion: SicdConversion | None,
    work_dir: str | os.PathLike | None,
    preparer: SicdPreparer | None,
) -> Iterator[str]:
    """Yield something ``rasterio`` can open for this item's ``asset``.

    For an amplitude raster that is the remote URL itself, so only the bytes of
    each tile cross the network. For a complex product it is a locally geocoded
    COG, built once and (with ``work_dir``) kept.
    """
    if asset.upper() not in COMPLEX_ASSETS:
        url = item.asset_href(asset)
        if not url:
            raise AssetNotFoundError(f"Item {item.id!r} has no resolvable URL for asset {asset!r}.")
        yield _open_path(url)
        return

    prepare = preparer or _prepare_sicd
    settings = conversion or SicdConversion()
    if work_dir is not None:
        kept = Path(work_dir)
        kept.mkdir(parents=True, exist_ok=True)
        yield str(prepare(item, asset, kept, settings))
        return
    with tempfile.TemporaryDirectory(prefix="umbra-chips-") as tmp:
        yield str(prepare(item, asset, Path(tmp), settings))


def _prepare_sicd(
    item: UmbraItem,
    asset: str,
    work_dir: Path,
    conversion: SicdConversion,
) -> Path:
    """Download a complex product and geocode it to a chippable COG.

    Both halves are resumable: :func:`umbra_py.download.download_asset` resumes a
    partial NITF, and a geocoded COG already present for these exact settings is
    reused rather than rebuilt. Needs the ``[convert]`` extra.
    """
    from .convert import sicd_to_geocoded_cog  # noqa: PLC0415
    from .download import download_asset  # noqa: PLC0415

    work_dir.mkdir(parents=True, exist_ok=True)
    dst = work_dir / f"{_safe_slug(item.id)}.{conversion.cache_key()}.tif"
    if dst.exists():
        return dst
    src = download_asset(item, asset, work_dir)
    return sicd_to_geocoded_cog(
        src,
        dst,
        decibels=False,
        gcp_grid=conversion.gcp_grid,
        resolution=conversion.resolution,
        resampling=conversion.resampling,
        projection_type=conversion.projection_type,
        dem=conversion.dem,
        geoid=conversion.geoid,
        rtc=conversion.rtc,
        rtc_reference_deg=conversion.rtc_reference_deg,
        rtc_model=conversion.rtc_model,
        calibration=conversion.calibration,
        bbox=conversion.bbox,
    )


@dataclass
class ChipRecord:
    """One training tile's manifest entry.

    Carries where the chip is (``path``, geographic ``bbox``, ``crs``,
    ``transform``, grid ``row`` / ``col``, source pixel ``window``), what the
    acquisition is (``item_id``, ``datetime``, ``place``, ``platform``,
    ``product_type``, ``polarizations``, ``incidence_angle_deg``, the
    ``resolution_*`` pair), and how usable it is (``valid_fraction`` -- the
    fraction of finite, positive pixels). ``license`` / ``attribution`` travel
    with every record.

    A chip cut from a complex product also carries what the conversion did to
    its pixels -- ``calibration`` and ``rtc_model``, read back from the geocoded
    raster's own provenance tags rather than from the request, so the record
    reports the processing that actually ran. Both are ``None`` for a chip read
    straight from an amplitude raster, and the full tag set travels in the chip
    GeoTIFF itself.
    """

    path: str
    item_id: str
    asset: str
    row: int
    col: int
    window: list[int]  # [col_off, row_off, width, height] in source pixels
    crs: str | None
    transform: list[float]  # 6-tuple affine of this chip
    bbox: list[float]  # geographic (EPSG:4326) min_lon, min_lat, max_lon, max_lat
    units: str
    valid_fraction: float
    datetime: str | None = None
    place: str | None = None
    platform: str | None = None
    product_type: str | None = None
    polarizations: list[str] = field(default_factory=list)
    incidence_angle_deg: float | None = None
    resolution_range_m: float | None = None
    resolution_azimuth_m: float | None = None
    calibration: str | None = None
    rtc_model: str | None = None
    license: str = DATA_LICENSE
    attribution: str = ATTRIBUTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "item_id": self.item_id,
            "asset": self.asset,
            "row": self.row,
            "col": self.col,
            "window": self.window,
            "crs": self.crs,
            "transform": self.transform,
            "bbox": self.bbox,
            "units": self.units,
            "valid_fraction": self.valid_fraction,
            "datetime": self.datetime,
            "place": self.place,
            "platform": self.platform,
            "product_type": self.product_type,
            "polarizations": self.polarizations,
            "incidence_angle_deg": self.incidence_angle_deg,
            "resolution_range_m": self.resolution_range_m,
            "resolution_azimuth_m": self.resolution_azimuth_m,
            "calibration": self.calibration,
            "rtc_model": self.rtc_model,
            "license": self.license,
            "attribution": self.attribution,
        }

    def to_feature(self) -> dict[str, Any]:
        """The chip as a GeoJSON ``Feature`` (footprint polygon + record props)."""
        min_lon, min_lat, max_lon, max_lat = self.bbox
        ring = [
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]
        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": self.to_dict(),
        }


@dataclass
class ChipDataset:
    """The result of a chipping run: the written chips plus their manifest.

    ``records`` are the :class:`ChipRecord` entries (also written to
    ``manifest_path``); the summary fields describe the run for a ``--json``
    caller or an agent deciding what to train on.
    """

    out_dir: str
    manifest_path: str | None
    records: list[ChipRecord]
    chip_size: int
    stride: int
    asset: str
    units: str
    fmt: str
    conversion: SicdConversion | None = None

    @property
    def chip_count(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        item_ids = sorted({r.item_id for r in self.records})
        # The conversion block appears only when one ran, so an unconverted
        # run's summary is unchanged by this field existing.
        extra: dict[str, Any] = (
            {"conversion": asdict(self.conversion)} if self.conversion is not None else {}
        )
        return {
            "out_dir": self.out_dir,
            "manifest": self.manifest_path,
            "chip_count": self.chip_count,
            "chip_size": self.chip_size,
            "stride": self.stride,
            "asset": self.asset,
            "units": self.units,
            "format": self.fmt,
            "item_count": len(item_ids),
            "items": item_ids,
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
            **extra,
        }


def _write_geotiff_chip(
    rasterio: Any,
    dest: Path,
    data: Any,
    crs: str | None,
    transform: Any,
    item: UmbraItem,
    asset: str,
    units: str,
    provenance: dict[str, str] | None = None,
) -> None:
    """Write one chip array as a single-band float32 GeoTIFF with geo + license
    tags, mirroring :func:`umbra_py.load.to_geotiff`'s profile so the chips read
    identically in QGIS / rasterio.

    ``provenance`` is the source raster's ``UMBRA_*`` conversion tags, copied
    through unchanged so a chip cut from a converted SICD says what its pixel
    values are (calibration, terrain-flattening model, DEM, scale) without the
    manifest beside it -- the same rule :func:`umbra_py.convert.conversion_tags`
    applies to the scene, applied to the tile.
    """
    from affine import Affine  # noqa: PLC0415

    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": Affine(*transform),
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(dest, "w", **profile) as dst:
        dst.write(data, 1)
        dst.update_tags(
            item_id=item.id,
            units=units,
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
            **(provenance or {}),
        )


def chip_item(
    item: UmbraItem,
    out_dir: str | os.PathLike,
    *,
    asset: str = "GEC",
    chip_size: int = 512,
    stride: int | None = None,
    db: bool = False,
    fmt: str = "geotiff",
    min_valid: float = 0.0,
    prefix: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    conversion: SicdConversion | None = None,
    work_dir: str | os.PathLike | None = None,
    preparer: SicdPreparer | None = None,
) -> list[ChipRecord]:
    """Cut one acquisition into fixed-size, georeferenced training tiles.

    Reads band 1 of the item's geocoded GeoTIFF (the ``GEC`` cloud-optimized
    GeoTIFF by default) one window at a time via HTTP range requests, and writes
    each full ``chip_size`` x ``chip_size`` tile to ``out_dir`` as a GeoTIFF (or
    a NumPy ``.npy`` array). Returns a :class:`ChipRecord` per written chip.

    With ``asset="SICD"`` the complex product is downloaded and geocoded first
    (see :class:`SicdConversion`) and the resulting COG is chipped by this same
    loop, so a training set can be cut from the full-resolution complex archive
    -- optionally terrain-orthorectified, terrain-flattened and radiometrically
    calibrated -- rather than only from the derived amplitude products.

    Parameters
    ----------
    item:
        The acquisition to chip.
    out_dir:
        Directory to write chips into (created if needed).
    asset:
        Which product to read. ``"GEC"`` (the default) and ``"CSI"`` are
        amplitude rasters, streamed tile by tile. ``"SICD"`` is the complex
        slant-plane product: it has no map grid, so it is fetched whole and
        geocoded before chipping (the ``[convert]`` extra). ``CPHD`` is phase
        history rather than a focused image and is not chippable.
    chip_size:
        Tile edge in pixels. Only full tiles are emitted; a partial strip along
        the right/bottom edge is dropped, so every chip has this exact shape.
    stride:
        Step between tile origins in pixels. Defaults to ``chip_size``
        (non-overlapping). A smaller stride overlaps tiles (dense inference /
        augmentation); it must be positive.
    db:
        Write the decibel (``20*log10(amplitude)``) scale instead of linear
        amplitude. Non-positive / nodata pixels become ``NaN`` either way.
    fmt:
        ``"geotiff"`` (georeferenced, the default) or ``"npy"`` (a bare
        ``float32`` array; the geo metadata lives in the manifest record).
    min_valid:
        Drop a tile whose fraction of valid (finite, positive) pixels is below
        this. ``0.0`` keeps every full tile; e.g. ``0.5`` drops mostly-nodata
        corners of a rotated footprint.
    prefix:
        Filename stem for this item's chips (defaults to a slug of ``item.id``).
        Chips are named ``<prefix>_r<row>_c<col>.<ext>``.
    bbox:
        Optional area of interest ``(min_lon, min_lat, max_lon, max_lat)`` in
        WGS-84 degrees (whatever the raster's own CRS is, as in
        :func:`umbra_py.to_stack`). Only tiles inside that window are cut, and
        ``row`` / ``col`` are numbered from its corner. For a complex ``asset``
        it also becomes the conversion's own clip, so the geocoding step is
        sized to the area of interest rather than to the scene — which is where
        the cost of chipping the complex archive actually lives. ``None`` chips
        the whole raster.
    conversion:
        How to geocode a complex ``asset`` before chipping it. Ignored for the
        amplitude rasters; defaults to :class:`SicdConversion`'s flat-earth
        geocoding.
    work_dir:
        Where the downloaded product and the geocoded COG are kept when chipping
        a complex ``asset``. ``None`` uses a temporary directory removed
        afterwards, so disk stays bounded to one scene; naming a directory keeps
        both files, which makes the expensive step resumable -- a re-run reuses a
        COG already geocoded with the same settings instead of rebuilding it.
    preparer:
        Override for the download-and-geocode step (the test seam; defaults to
        :func:`_prepare_sicd`).

    Returns
    -------
    list[ChipRecord]
        One record per written chip, in row-major order.
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    from rasterio.warp import transform_bounds  # noqa: PLC0415
    from rasterio.windows import Window  # noqa: PLC0415

    if chip_size < 1:
        raise ValueError(f"chip_size must be >= 1, got {chip_size}.")
    step = chip_size if stride is None else stride
    if step < 1:
        raise ValueError(f"stride must be >= 1, got {step}.")
    fmt = fmt.lower()
    if fmt not in ("geotiff", "npy"):
        raise ValueError(f"fmt must be 'geotiff' or 'npy', got {fmt!r}.")
    if not 0.0 <= min_valid <= 1.0:
        raise ValueError(f"min_valid must be in [0, 1], got {min_valid}.")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stem = prefix or _safe_slug(item.id)
    units = "dB" if db else "amplitude"
    rng, azi = item.resolution
    dt = item.datetime

    records: list[ChipRecord] = []
    if bbox is not None:
        # One decision, applied in both places: a complex product is geocoded
        # only over the area of interest, and every asset is then tiled over it.
        conversion = replace(conversion or SicdConversion(), bbox=_as_bbox(bbox))
    source_cm = _chip_source(
        item, asset, conversion=conversion, work_dir=work_dir, preparer=preparer
    )
    with source_cm as source, rasterio.open(source) as src:
        nodata = src.nodata
        crs = src.crs
        crs_str = crs.to_string() if crs else None
        provenance = _provenance_tags(src)
        calibration = _reported_step(provenance, "CALIBRATION")
        rtc_model = _reported_step(provenance, "RTC_MODEL")
        if bbox is None:
            row0, col0, row_stop, col_stop = 0, 0, src.height, src.width
        else:
            row0, col0, row_stop, col_stop = _clip_pixel_window(src, bbox)
        rows = range(row0, row_stop - chip_size + 1, step)
        cols = range(col0, col_stop - chip_size + 1, step)
        for row, r0 in enumerate(rows):
            for col, c0 in enumerate(cols):
                window = Window(c0, r0, chip_size, chip_size)
                data = src.read([1], window=window)[0].astype("float32")

                invalid = ~np.isfinite(data)
                if nodata is not None:
                    invalid |= data == nodata
                invalid |= data <= 0
                valid_fraction = float(1.0 - invalid.mean())
                if valid_fraction < min_valid:
                    continue

                if db:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        data = np.where(invalid, np.nan, 20.0 * np.log10(data)).astype("float32")
                else:
                    data = np.where(invalid, np.nan, data).astype("float32")

                transform = src.window_transform(window)
                left, top = transform.c, transform.f
                right, bottom = transform * (chip_size, chip_size)
                if crs is not None:
                    geo_bounds = transform_bounds(crs, "EPSG:4326", left, bottom, right, top)
                else:
                    geo_bounds = (left, bottom, right, top)

                name = f"{stem}_r{row:03d}_c{col:03d}"
                if fmt == "geotiff":
                    chip_path = out_path / f"{name}.tif"
                    _write_geotiff_chip(
                        rasterio,
                        chip_path,
                        data,
                        crs_str,
                        tuple(transform)[:6],
                        item,
                        asset,
                        units,
                        provenance,
                    )
                else:
                    chip_path = out_path / f"{name}.npy"
                    np.save(chip_path, data)

                records.append(
                    ChipRecord(
                        path=chip_path.name,
                        item_id=item.id,
                        asset=asset,
                        row=row,
                        col=col,
                        window=[c0, r0, chip_size, chip_size],
                        crs=crs_str,
                        transform=[float(v) for v in tuple(transform)[:6]],
                        bbox=[float(v) for v in geo_bounds],
                        units=units,
                        valid_fraction=round(valid_fraction, 6),
                        datetime=dt.isoformat() if dt else None,
                        place=item.task,
                        platform=item.platform,
                        product_type=item.product_type,
                        polarizations=item.polarizations,
                        incidence_angle_deg=item.incidence_angle,
                        resolution_range_m=rng,
                        resolution_azimuth_m=azi,
                        calibration=calibration,
                        rtc_model=rtc_model,
                    )
                )
    return records


def _chip_to_stac_item(record: ChipRecord) -> dict[str, Any]:
    """Shape one chip record as a minimal STAC Item for stac-geoparquet.

    The chip is naturally item-shaped: it has an id (its filename stem, unique
    across a dataset), a footprint geometry and bbox, an acquisition datetime,
    and the record fields as properties. The chip file itself is the item's one
    ``data`` asset, so a parquet consumer gets from a row back to the raster.
    Property names mirror :meth:`ChipRecord.to_dict` (minus ``bbox``, which is
    the STAC bbox, and ``datetime``, promoted to the STAC ``properties.datetime``)
    so the parquet columns match the ``.jsonl`` / ``.geojson`` fields.
    """
    min_lon, min_lat, max_lon, max_lat = record.bbox
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    props = {k: v for k, v in record.to_dict().items() if k not in ("bbox", "datetime")}
    props["datetime"] = record.datetime  # STAC core (may be null)
    asset_type = (
        "image/tiff; application=geotiff"
        if record.path.lower().endswith((".tif", ".tiff"))
        else "application/octet-stream"
    )
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": Path(record.path).stem,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "bbox": [float(v) for v in record.bbox],
        "properties": props,
        "links": [],
        "assets": {"data": {"href": record.path, "type": asset_type, "roles": ["data"]}},
    }


def write_manifest_parquet(records: list[ChipRecord], path: str | os.PathLike) -> Path:
    """Write chip records as a stac-geoparquet manifest (needs the ``[export]`` extra).

    Each chip becomes one STAC Item row (footprint geometry + record properties),
    so a large chip set is queryable by DuckDB / geopandas / pyarrow without
    reading every line -- what the ``.jsonl`` / ``.geojson`` manifests can't offer
    at scale. Reuses the same ``stac_geoparquet.arrow`` writer as
    :func:`umbra_py.export.export_geoparquet`.
    """
    from .export import _require as _require_export  # noqa: PLC0415

    _require_export("stac_geoparquet")
    import stac_geoparquet.arrow  # noqa: PLC0415

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    docs = [_chip_to_stac_item(r) for r in records]
    reader = stac_geoparquet.arrow.parse_stac_items_to_arrow(docs)
    stac_geoparquet.arrow.to_parquet(reader, path)
    return path


def write_manifest(records: list[ChipRecord], path: str | os.PathLike) -> Path:
    """Write chip records to a manifest, format chosen by ``path``'s extension.

    ``.jsonl`` (default) writes one JSON record per line -- the standard ML
    manifest format, streamable and append-friendly. ``.geojson`` writes a
    ``FeatureCollection`` of chip footprint polygons (each carrying the full
    record as properties) for QGIS / geopandas; both are stdlib-only.
    ``.parquet`` writes a stac-geoparquet table (one column-oriented file DuckDB /
    geopandas can query without loading every line) and needs the ``[export]``
    extra.
    """
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return write_manifest_parquet(records, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".geojson":
        fc = {
            "type": "FeatureCollection",
            "features": [r.to_feature() for r in records],
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        }
        path.write_text(json.dumps(fc), encoding="utf-8")
    else:
        with path.open("w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record.to_dict()) + "\n")
    return path


def write_chips(
    items: Iterable[UmbraItem],
    out_dir: str | os.PathLike,
    *,
    asset: str = "GEC",
    chip_size: int = 512,
    stride: int | None = None,
    db: bool = False,
    fmt: str = "geotiff",
    min_valid: float = 0.0,
    bbox: tuple[float, float, float, float] | None = None,
    manifest: str | None = "manifest.jsonl",
    progress: ProgressFn | None = None,
    conversion: SicdConversion | None = None,
    work_dir: str | os.PathLike | None = None,
    preparer: SicdPreparer | None = None,
) -> ChipDataset:
    """Chip a whole search result into a training dataset with a manifest.

    Iterates ``items``, calls :func:`chip_item` on each, and writes a combined
    manifest (``out_dir/manifest``) describing every chip. Returns a
    :class:`ChipDataset` summarising the run.

    ``manifest`` is the manifest filename inside ``out_dir`` (``.jsonl``,
    ``.geojson``, or ``.parquet`` -- the last needs the ``[export]`` extra);
    pass ``None`` to skip writing it and just collect the records. ``progress``
    is called ``(index, total, item, chips_written)`` after each item, for a CLI
    progress line.

    ``bbox`` restricts every acquisition to one area of interest (see
    :func:`chip_item`) -- the usual shape of a dataset build, where the site is
    the subject and the scenes are just the passes over it.

    ``conversion`` / ``work_dir`` / ``preparer`` apply when ``asset`` is a
    complex product (see :func:`chip_item`). Each acquisition is prepared and
    chipped in turn, so a run over many SICDs holds one scene on disk at a time
    unless ``work_dir`` is set to keep them.
    """
    out_path = Path(out_dir)
    items = list(items)
    # Resolve the default here rather than per item, so the dataset summary
    # reports the settings that actually ran even when the caller passed none.
    if asset.upper() in COMPLEX_ASSETS:
        conversion = conversion or SicdConversion()
        if bbox is not None:
            conversion = replace(conversion, bbox=_as_bbox(bbox))
    else:
        conversion = None
    records: list[ChipRecord] = []
    for i, item in enumerate(items):
        recs = chip_item(
            item,
            out_path,
            asset=asset,
            chip_size=chip_size,
            stride=stride,
            db=db,
            fmt=fmt,
            min_valid=min_valid,
            bbox=bbox,
            conversion=conversion,
            work_dir=work_dir,
            preparer=preparer,
        )
        records.extend(recs)
        if progress is not None:
            progress(i + 1, len(items), item, len(recs))

    manifest_path: str | None = None
    if manifest is not None:
        written = write_manifest(records, out_path / manifest)
        manifest_path = str(written)

    return ChipDataset(
        out_dir=str(out_path),
        manifest_path=manifest_path,
        records=records,
        chip_size=chip_size,
        stride=chip_size if stride is None else stride,
        asset=asset,
        units="dB" if db else "amplitude",
        fmt=fmt.lower(),
        conversion=conversion,
    )
