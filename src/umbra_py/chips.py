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
- **A batch says which of its scenes the noise estimate should not be trusted
  on.** The inferred noise floors (``--noise-model estimated`` /
  ``estimated-range``) work because a SAR scene's dark surfaces are a different
  population from its backscatter, and :class:`umbra_py.convert.NoiseSubtraction`
  measures how true that was of each scene. ``umbra convert`` prints those
  numbers for the one raster it writes; a chip run converts many, so they arrive
  here twice over -- per chip, as :class:`ChipRecord` fields a training loader
  can filter the manifest on, and once for the run as a
  :class:`NoiseSummary` roll-up that counts the scenes with too little dark
  ground to read. Neither is a refusal: a uniformly bright scene is legitimate
  imagery, and the honest fix where it matters is a measured floor.

The manifest is machine-readable first: ``.jsonl`` (one chip record per line --
the standard ML manifest format) or ``.geojson`` (a ``FeatureCollection`` of
chip footprints for QGIS / geopandas), both stdlib-only. A third format,
``.parquet``, writes the same chip footprints as `stac-geoparquet
<https://stac-geoparquet.org/>`__ -- one column-oriented file DuckDB, geopandas
or pyarrow can query without loading every line, the format a *large* chip set
wants (the same plumbing :mod:`umbra_py.export` uses for the catalog snapshot).
It needs the ``[export]`` extra alongside ``[load]``.

Whatever the manifest's format, a run that could not include every acquisition
it was offered writes a ``skipped.jsonl`` sidecar beside it -- one line per
left-out pass, in the product's own words. The manifest describes the tiles that
exist; the sidecar is the only thing in the directory that says which ones were
meant to and do not, and it is written only when there is something to say.

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
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

from .constants import ATTRIBUTION, DATA_LICENSE

# The one eager import from `convert`: a default has to exist when the dataclass
# below is defined, and duplicating the number here is exactly the drift the
# shared constant prevents. `convert`'s module level is stdlib-only (its heavy
# dependencies are behind `_require`), so this pulls in no extra.
from .convert import SPECKLE_WINDOW_DEFAULT
from .exceptions import AssetNotFoundError, UnsupportedMeasurementError
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

#: Progress callback for the pre-download check: ``(index, total, item, result)``,
#: where ``result`` is a :class:`umbra_py.preflight.PreflightResult`. Typed loosely
#: so importing this module never pulls :mod:`umbra_py.preflight` in.
PreflightProgressFn = Callable[[int, int, UmbraItem, Any], None]

#: Edge of the grid of full-resolution windows :func:`_scene_speckle` samples to
#: establish a scene's speckle statistics: 3 means up to nine windows spread
#: evenly over the area being chipped. It is a *sample* because the alternative
#: is reading the whole product — the thing streaming a GEC tile by tile exists
#: to avoid — and a grid rather than a random draw because a chip set has to be
#: reproducible: the same acquisition and the same area give the same windows,
#: so they give the same filter.
_SPECKLE_SAMPLE_GRID = 3

#: Edge, in pixels, of each of those windows. Large enough to hold many
#: measuring blocks (:data:`umbra_py.convert._ENL_BLOCK`, sized to the filter
#: window), small enough that nine of them are a handful of megabytes rather
#: than a download.
_SPECKLE_SAMPLE_SIZE = 512


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
    noise_subtract: bool = False
    noise_model: str = "measured"
    speckle_filter: str | None = None
    speckle_window: int = SPECKLE_WINDOW_DEFAULT
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


def _reported_number(provenance: dict[str, str], name: str) -> float | None:
    """One numeric diagnostic from the provenance tags, as ``None`` when absent.

    The diagnostics :class:`umbra_py.convert.NoiseSubtraction` records are
    written only by the step that computed them, so unlike a processing step
    there is no ``"none"`` sentinel to translate -- an absent tag means the
    subtraction those numbers describe did not run. GeoTIFF metadata is strings,
    so the float lives here rather than in every caller.
    """
    from .convert import PROVENANCE_TAG_PREFIX  # noqa: PLC0415

    value = provenance.get(f"{PROVENANCE_TAG_PREFIX}{name}")
    return None if value is None else float(value)


def _speckle_provenance(scene: Any) -> dict[str, str]:
    """A scene's :class:`umbra_py.convert.SpeckleFiltering` as ``UMBRA_*`` tags.

    ``umbra convert``'s own keys rather than a second vocabulary, for the reason
    :func:`umbra_py.load._filtered_provenance` gives: a tile whose cells were
    averaged over an N-pixel window *is* an N-window-filtered raster, so every
    consumer of those tags -- the refusal to difference a filtered pass against
    an unfiltered one, ``stack_stats``' caveat, a reader running ``gdalinfo`` on
    a chip -- works on it unchanged. The values are written by the same
    :func:`umbra_py.convert.conversion_tags` formatter, so a chip cut from a
    filtered GEC and one cut from a filtered SICD read identically.
    """
    from .convert import PROVENANCE_TAG_PREFIX, _speckle_detail_tags  # noqa: PLC0415

    tags = {
        "SPECKLE_FILTER": scene.filter,
        **_speckle_detail_tags(
            window=scene.window,
            enl_before=scene.enl_before,
            enl_after=scene.enl_after,
            looks=scene.looks,
        ),
    }
    return {f"{PROVENANCE_TAG_PREFIX}{key}": value for key, value in tags.items()}


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


def _invalid_mask(np: Any, data: Any, nodata: float | None) -> Any:
    """Which pixels of a read are not a measurement: non-finite, nodata, or ``<= 0``.

    One definition, because the speckle filter and the chip loop have to agree on
    it: a pixel the loop counts as invalid must be one the filter excluded from
    its neighbours' windows, or an edge chip's values would be dragged toward a
    zero that never was a return.
    """
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    invalid |= data <= 0
    return invalid


class _ChipSpeckle(NamedTuple):
    """The speckle filter every tile of one acquisition is cut through.

    ``name`` and ``window`` are checked at the call (before any bytes are read),
    and ``scene`` is what the filter's own parameters and diagnostics were read
    from -- :func:`_scene_speckle`, once per acquisition. Carrying the scene
    record here rather than re-reading it per tile is the whole design: ``lee``'s
    speckle parameter is a property of the *product's* processing, so a tile that
    read it off its own 512 pixels would filter a chip over water differently
    from the one beside it over a city, and a training set would carry that seam.
    """

    name: str
    window: int
    scene: Any  # umbra_py.convert.SpeckleFiltering


def _resolve_chip_speckle(name: str | None, window: int) -> tuple[str, int] | None:
    """Check a requested chip-side speckle filter before any bytes are read.

    Mirrors :func:`umbra_py.load._resolve_speckle`: a misspelt filter or an even
    window should fail at the call that got it wrong, not part-way through a run
    that has already streamed scenes.
    """
    if name is None:
        return None
    from .convert import SPECKLE_FILTERS, _check_speckle_window  # noqa: PLC0415

    lowered = name.lower()
    if lowered not in SPECKLE_FILTERS:
        raise ValueError(
            f"Unknown speckle_filter {name!r}; choose one of {', '.join(SPECKLE_FILTERS)}."
        )
    return lowered, _check_speckle_window(window)


def _with_conversion_speckle(
    conversion: SicdConversion | None, name: str, window: int
) -> SicdConversion:
    """Route a chip-side speckle request into the conversion, for a complex asset.

    A ``SICD`` is filtered *before* it is geocoded, in the radar's own image
    space, where speckle is one independent sample per pixel -- so the honest
    place for the flag on that path is :class:`SicdConversion`, not the tile
    loop. One request, placed where it is most correct for the asset it is
    filtering; naming it twice and differently is refused rather than silently
    resolved, since only the caller knows which they meant.
    """
    settings = conversion or SicdConversion()
    already = settings.speckle_filter
    if already is not None and (already != name or settings.speckle_window != window):
        raise ValueError(
            f"Conflicting speckle filters for a complex asset: the conversion asks for "
            f"{already!r} over a {settings.speckle_window}x{settings.speckle_window} window "
            f"and the chipper for {name!r} over {window}x{window}. A SICD is filtered once, "
            "in image space before it is geocoded, so name it in one place."
        )
    return replace(settings, speckle_filter=name, speckle_window=window)


def _sample_offsets(start: int, stop: int, size: int, count: int) -> list[int]:
    """Up to ``count`` evenly spread window origins covering ``[start, stop)``.

    Collapses to a single origin where the span is no wider than one window, and
    de-duplicates where the spread would repeat one -- so a small raster is
    sampled once rather than nine times over.
    """
    if stop - start <= size or count < 2:
        return [start]
    last = stop - size
    return sorted({start + round(i * (last - start) / (count - 1)) for i in range(count)})


def _scene_speckle(src: Any, name: str, window: int, extent: tuple[int, int, int, int]) -> Any:
    """Read one acquisition's speckle statistics off a sample of its own windows.

    Returns a :class:`umbra_py.convert.SpeckleFiltering` measured for the scene:
    the equivalent number of looks before and after the filter, and -- for
    ``"lee"`` -- the looks the filter will be told the speckle has. Every tile is
    then cut through :func:`umbra_py.convert._filter_speckle` with that one
    ``looks``, so the whole chip set is filtered by the same arithmetic with the
    same parameter and two overlapping tiles cannot disagree about the ground
    they share.

    It is a sample rather than a whole-scene read because the chipper's promise
    is that only the bytes of the tiles cross the network. The blocks of every
    sampled window are *pooled* before the percentile is taken
    (:func:`umbra_py.convert._block_enl_ratios`), which is what makes the result
    an estimate of the scene rather than an average of nine estimates -- and the
    grid is fixed, so the same acquisition gives the same number on every run.

    The pair it reports describes the sampled windows, which is the honest scope:
    the same caveat :func:`umbra_py.convert._estimate_enl` carries about texture
    biasing a block's ENL down applies, so the *ratio* is the number to trust.
    """
    np = _require("numpy")
    from rasterio.windows import Window  # noqa: PLC0415

    from .convert import (  # noqa: PLC0415
        _ENL_BLOCK,
        _ENL_BLOCK_WINDOWS,
        _ENL_PERCENTILE,
        SpeckleFiltering,
        _block_enl_ratios,
        _boxcar_power,
        _detected_power,
        _lee_power,
    )

    row0, col0, row_stop, col_stop = extent
    size = _SPECKLE_SAMPLE_SIZE
    # The same block size `_filter_speckle` would use, so this scene's numbers are
    # comparable with a converted raster's rather than differently biased.
    block = max(_ENL_BLOCK, _ENL_BLOCK_WINDOWS * window)
    nodata = src.nodata

    powers = []
    for r0 in _sample_offsets(row0, row_stop, size, _SPECKLE_SAMPLE_GRID):
        for c0 in _sample_offsets(col0, col_stop, size, _SPECKLE_SAMPLE_GRID):
            height = min(size, row_stop - r0)
            width = min(size, col_stop - c0)
            patch = src.read([1], window=Window(c0, r0, width, height))[0].astype("float32")
            patch = np.where(_invalid_mask(np, patch, nodata), np.nan, patch)
            powers.append(_detected_power(patch, decibels=False))

    def _pooled(arrays: list[Any]) -> float | None:
        ratios = np.concatenate(arrays) if arrays else np.empty(0, dtype="float64")
        return float(np.percentile(ratios, _ENL_PERCENTILE)) if ratios.size else None

    enl_before = _pooled([_block_enl_ratios(p, block=block) for p in powers])

    looks: float | None = None
    if name == "lee":
        # Clamped at single-look for the reason `_filter_speckle` clamps it: no
        # product has fewer looks than one, so a lower read is the estimator
        # meeting texture, and believing it is licence to smooth structure away.
        looks = max(enl_before, 1.0) if enl_before is not None else 1.0

    after = []
    for power in powers:
        filtered = (
            _lee_power(power, window, looks=looks)
            if looks is not None
            else _boxcar_power(power, window)
        )
        after.append(_block_enl_ratios(np.where(np.isfinite(power), filtered, np.nan), block=block))
    return SpeckleFiltering(
        filter=name,
        window=window,
        enl_before=enl_before,
        enl_after=_pooled(after),
        looks=looks,
    )


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
        noise_subtract=conversion.noise_subtract,
        noise_model=conversion.noise_model,
        speckle_filter=conversion.speckle_filter,
        speckle_window=conversion.speckle_window,
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
    its pixels -- ``calibration``, ``noise_subtraction``, ``speckle_filter`` /
    ``speckle_window`` and ``rtc_model``, read back from the geocoded raster's own
    provenance tags rather than from the request, so the record reports the
    processing that actually ran. ``calibration``, ``noise_subtraction`` and
    ``rtc_model`` are ``None`` for a chip read straight from an amplitude raster
    -- those steps need a complex product -- and the full tag set travels in the
    chip GeoTIFF itself.

    The shape is public API and published as
    ``docs/schemas/chip-record.schema.json``: it is what a training loader parses
    without ever having printed it, in whichever of the three manifest formats it
    reads (one ``.jsonl`` line, one ``.geojson`` feature's ``properties``, one
    ``.parquet`` row -- the same record).

    The speckle pair is the one that says what a chip's *resolution* is as
    opposed to its pixel size: a 5x5-filtered chip resolves ground five pixels
    across, which is what a model trained on it can learn to see. It is filled in
    on **either** path, because an amplitude raster can be filtered too -- on the
    published GEC the tiles themselves are averaged (see :func:`chip_item`'s
    ``speckle_filter``), on a SICD the scene is, in the radar's own image space
    before it is geocoded.

    ``speckle_enl_before`` / ``speckle_enl_after`` / ``speckle_looks`` are that
    filter's *diagnostics* (see :class:`umbra_py.convert.SpeckleFiltering`): the
    scene's equivalent number of looks either side of the window, and the looks
    ``"lee"`` assumed for the speckle it was separating from structure. Like the
    noise diagnostics they describe the acquisition a chip was cut from rather
    than the chip, so every chip of one scene carries the same three; the ratio
    of the pair is what says whether the resolution the window spent bought
    anything, and either level on its own reads low on a textured scene.

    ``noise_floored_fraction`` and ``noise_floor_margin_db`` come from the same
    tags and are the noise subtraction's two *diagnostics* (see
    :class:`umbra_py.convert.NoiseSubtraction`): how much of the scene the floor
    drove to the sensor's sensitivity limit, and -- for an inferred floor -- how
    far the scene's own median power sat above it. They describe the scene the
    chip was cut from rather than the chip, so every chip of one acquisition
    carries the same pair; a training loader that wants to drop the scenes whose
    dark tail was ground rather than receiver can filter the manifest on the
    second without opening a raster.
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
    noise_subtraction: str | None = None
    noise_floored_fraction: float | None = None
    noise_floor_margin_db: float | None = None
    speckle_filter: str | None = None
    speckle_window: int | None = None
    speckle_enl_before: float | None = None
    speckle_enl_after: float | None = None
    speckle_looks: float | None = None
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
            "noise_subtraction": self.noise_subtraction,
            "noise_floored_fraction": self.noise_floored_fraction,
            "noise_floor_margin_db": self.noise_floor_margin_db,
            "speckle_filter": self.speckle_filter,
            "speckle_window": self.speckle_window,
            "speckle_enl_before": self.speckle_enl_before,
            "speckle_enl_after": self.speckle_enl_after,
            "speckle_looks": self.speckle_looks,
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


@dataclass(frozen=True)
class NoiseSummary:
    """What the noise subtraction did across a whole chipping run.

    :class:`umbra_py.convert.NoiseSubtraction`'s diagnostics are per scene, and
    ``umbra convert`` prints them for the one raster it wrote. A chip run
    converts *many* scenes, so the equivalent there is not a line each -- it is
    the question a dataset builder actually has: **were any of these scenes ones
    the estimator should not have been used on?** The inferred floors work
    because a SAR scene's dark surfaces are a different population from its
    backscatter; where they aren't, the subtraction takes real backscatter off,
    and the tell is a narrow margin between the floor and the scene's median
    power. That is a property of some scenes in a batch and not others, which is
    exactly what a roll-up is for.

    Counted per *acquisition* rather than per chip: the numbers describe the
    scene each chip was cut from, so counting chips would weight a wide scene
    more heavily than a narrow one for no reason. Scenes that produced no chips
    (everything dropped by ``min_valid``) are not in the batch this describes.

    Attributes
    ----------
    scenes:
        Acquisitions in this run whose chips carry a noise subtraction.
    models:
        The distinct ``UMBRA_NOISE_SUBTRACTION`` values across them, sorted --
        normally one, since a run converts every scene the same way.
    margin_scenes:
        How many of ``scenes`` reported a margin at all. Only the inferred
        floors do: a measured floor is the product's own metadata and assumes
        nothing about the scene, so it has nothing to report.
    low_margin_scenes:
        How many of ``margin_scenes`` sat below ``margin_warn_db``. This is the
        number the roll-up exists for.
    margin_warn_db:
        The advisory threshold applied (``convert.NOISE_MARGIN_WARN_DB``).
    min_margin_db:
        The narrowest margin in the batch -- the worst scene, ``None`` when no
        scene reported one.
    max_floored_fraction:
        The largest fraction of a scene the floor drove to the sensor's
        sensitivity limit. Reported by both models.
    """

    scenes: int
    models: list[str]
    margin_scenes: int
    low_margin_scenes: int
    margin_warn_db: float
    min_margin_db: float | None = None
    max_floored_fraction: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summarise_noise(records: Iterable[ChipRecord]) -> NoiseSummary | None:
    """Roll a run's per-scene noise diagnostics up into one :class:`NoiseSummary`.

    ``None`` when no chip in the run came from a scene that subtracted a floor,
    which keeps the summary out of an ordinary GEC run's output entirely.
    """
    from .convert import NOISE_MARGIN_WARN_DB  # noqa: PLC0415

    # One entry per acquisition: the diagnostics are the scene's, so every chip
    # of one item repeats them.
    scenes: dict[str, ChipRecord] = {}
    for record in records:
        if record.noise_subtraction is not None:
            scenes.setdefault(record.item_id, record)
    if not scenes:
        return None

    margins = [
        r.noise_floor_margin_db for r in scenes.values() if r.noise_floor_margin_db is not None
    ]
    floored = [
        r.noise_floored_fraction for r in scenes.values() if r.noise_floored_fraction is not None
    ]
    return NoiseSummary(
        scenes=len(scenes),
        models=sorted({str(r.noise_subtraction) for r in scenes.values()}),
        margin_scenes=len(margins),
        low_margin_scenes=sum(1 for m in margins if m < NOISE_MARGIN_WARN_DB),
        margin_warn_db=NOISE_MARGIN_WARN_DB,
        min_margin_db=min(margins) if margins else None,
        max_floored_fraction=max(floored) if floored else None,
    )


@dataclass(frozen=True)
class SpeckleSummary:
    """What the speckle filter did across a whole chipping run.

    The same shape :class:`NoiseSummary` has, for the same reason: the
    diagnostics are per scene, and what a dataset builder actually wants to know
    about a batch is **did the window buy anything, and on how many of these
    scenes did it not?** A filter's job is to raise the equivalent number of
    looks, and the honest report of that is the ratio -- both levels read low on
    a textured scene, so ``enl_after`` alone says little.

    Counted per *acquisition*, since the numbers describe the scene each chip was
    cut from. Never a refusal: a scene that is textured everywhere is legitimate
    imagery, and a small gain there is the outcome rather than a fault.

    Attributes
    ----------
    scenes:
        Acquisitions in this run whose chips were speckle-filtered.
    filters, windows:
        The distinct filters and window edges across them, sorted -- normally one
        of each, since a run filters every scene the same way.
    gain_scenes:
        How many of ``scenes`` reported an ENL either side of the filter, so a
        gain could be computed at all. A scene smaller than one measuring block,
        or with too little finite ground in it, reports neither.
    low_gain_scenes:
        How many of ``gain_scenes`` came out below ``gain_warn``. This is the
        number the roll-up exists for.
    gain_warn:
        The advisory threshold applied (``convert.SPECKLE_ENL_GAIN_WARN``).
    min_gain, median_gain:
        The worst and the typical ``enl_after / enl_before`` in the batch,
        ``None`` when no scene reported a pair.
    """

    scenes: int
    filters: list[str]
    windows: list[int]
    gain_scenes: int
    low_gain_scenes: int
    gain_warn: float
    min_gain: float | None = None
    median_gain: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summarise_speckle(records: Iterable[ChipRecord]) -> SpeckleSummary | None:
    """Roll a run's per-scene speckle diagnostics up into one :class:`SpeckleSummary`.

    ``None`` when no chip in the run was filtered, which keeps the summary out of
    an ordinary unfiltered run's output entirely.
    """
    from .convert import SPECKLE_ENL_GAIN_WARN  # noqa: PLC0415

    scenes: dict[str, ChipRecord] = {}
    for record in records:
        if record.speckle_filter is not None:
            scenes.setdefault(record.item_id, record)
    if not scenes:
        return None

    gains = sorted(
        r.speckle_enl_after / r.speckle_enl_before
        for r in scenes.values()
        if r.speckle_enl_before and r.speckle_enl_after
    )
    return SpeckleSummary(
        scenes=len(scenes),
        filters=sorted({str(r.speckle_filter) for r in scenes.values()}),
        windows=sorted({int(r.speckle_window) for r in scenes.values() if r.speckle_window}),
        gain_scenes=len(gains),
        low_gain_scenes=sum(1 for g in gains if g < SPECKLE_ENL_GAIN_WARN),
        gain_warn=SPECKLE_ENL_GAIN_WARN,
        min_gain=gains[0] if gains else None,
        median_gain=gains[len(gains) // 2] if gains else None,
    )


@dataclass(frozen=True)
class SkippedAcquisition:
    """One acquisition left out of a run because it could not support the request.

    A dataset built with ``skip_unsupported=True`` is a dataset with a hole in
    it, so the hole is part of the result rather than a line on a console
    somebody may not have been watching: ``item_id`` and ``datetime`` say which
    pass is missing, ``reason`` is the refusal's own words (the product's
    metadata, not a paraphrase), and ``hint`` carries the recovery step where
    the refusal named one.

    What produces one is a fact about a *product*, and only that: a
    :class:`~umbra_py.exceptions.UnsupportedMeasurementError` (the metadata was
    read, and it cannot support the request) or, from a preflight, an
    :class:`~umbra_py.exceptions.UnreadableProductError` (there is no readable
    product at the acquisition's href to ask). Both are final, which is what
    makes carrying on to the next scene a defensible response, where carrying on
    past an unknown error -- a download failure, a corrupt file, a transport
    hiccup -- would be a way of hiding one.

    ``stage`` says *when* it was discovered, because that is the one thing the
    routes to it do not share. ``"conversion"`` means the product was downloaded
    and then refused (``skip_unsupported=True``); ``"preflight"`` means its
    metadata was read over the wire and it was never downloaded at all
    (``preflight=True``). The reason is the product's own words either way -- the
    conversion's own check, or the reader's -- so the dataset's hole is described
    the same and only its cost differs.

    Published as ``docs/schemas/chip-skipped.schema.json`` -- one line of the
    ``skipped.jsonl`` sidecar and one entry of the dataset summary's ``skipped``
    array, which is one contract because it is one record.
    """

    item_id: str
    reason: str
    datetime: str | None = None
    hint: str | None = None
    stage: str = "conversion"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "datetime": self.datetime,
            "reason": self.reason,
            "hint": self.hint,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class PreflightSummary:
    """What asking the archive first cost a chip run, and what it saved.

    A preflight is only worth wiring into a batch if the saving is visible, so
    the two numbers that make the case are part of the result rather than a line
    on a console: ``bytes_read`` is what the whole check cost (product headers
    and SICD XML, a few tens of kilobytes each) and ``product_bytes_skipped`` is
    the download it removed -- the products that were dropped, summed, as their
    own sources declared them.

    The claim is deliberately narrow. A supported pass is downloaded anyway, so
    its header read is pure overhead; only the *dropped* passes are a saving, and
    that is the number reported.

    The two error counts are the two things a failed read can mean, and they are
    counted apart because the run does different things with them.
    ``unreadable`` is a read that failed on the *wire* — those passes are
    **kept**, because a transport failure is not a product saying it cannot
    answer, and dropping a scene over a blip would be exactly the silent hole
    this design avoids. ``missing`` is a read that failed on the *product*:
    nothing at the href, or something at it that is not a SICD. Those are
    dropped, and are part of ``skipped``. Keeping them was never the cautious
    choice it looked like — a pass with no readable product fails inside
    ``chip_item`` as a plain read error rather than an
    :class:`~umbra_py.exceptions.UnsupportedMeasurementError`, so
    ``skip_unsupported`` does not catch it and a run that preflighted ends on a
    pass the preflight had already ruled out.
    """

    checked: int
    supported: int
    skipped: int
    unreadable: int
    bytes_read: int
    product_bytes_skipped: int | None = None
    missing: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "supported": self.supported,
            "skipped": self.skipped,
            "unreadable": self.unreadable,
            "missing": self.missing,
            "bytes_read": self.bytes_read,
            "product_bytes_skipped": self.product_bytes_skipped,
        }


@dataclass
class ChipDataset:
    """The result of a chipping run: the written chips plus their manifest.

    ``records`` are the :class:`ChipRecord` entries (also written to
    ``manifest_path``); the summary fields describe the run for a ``--json``
    caller or an agent deciding what to train on.

    ``skipped`` is what the run could *not* include: the acquisitions whose own
    metadata could not support the measurement that was asked for, present when
    ``write_chips(skip_unsupported=True)`` let the run carry on past them or when
    ``write_chips(preflight=True)`` dropped them before downloading them. An
    empty tuple is the default and means what it says -- every acquisition
    offered was chipped.

    ``preflight`` is the roll-up of that pre-download check when one ran: what
    reading the archive's headers cost, and the download it removed.

    ``skipped_path`` is where that hole was *written*, when there was one --
    the sidecar beside the manifest (see :func:`write_skipped_manifest`), so a
    loader reading the directory rather than the run can see it too. ``None``
    when nothing was skipped, which is the same thing the empty ``skipped``
    tuple says.

    :meth:`to_dict` is what ``umbra chips --json`` prints, and its shape is
    published as ``docs/schemas/chip-dataset.schema.json``. Its conditional keys
    are part of that contract: ``conversion``, ``noise``, ``speckle``,
    ``skipped`` and ``preflight`` appear only when the run had something to say
    with them, so an ordinary raster run's payload is unchanged by any of those
    features existing.
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
    skipped: tuple[SkippedAcquisition, ...] = ()
    preflight: PreflightSummary | None = None
    skipped_path: str | None = None

    @property
    def chip_count(self) -> int:
        return len(self.records)

    @property
    def noise(self) -> NoiseSummary | None:
        """The run's noise-subtraction roll-up, or ``None`` when none ran.

        Derived from ``records`` rather than accumulated during the run, so the
        summary can never disagree with the manifest it sits beside.
        """
        return _summarise_noise(self.records)

    @property
    def speckle(self) -> SpeckleSummary | None:
        """The run's speckle-filtering roll-up, or ``None`` when none ran.

        Derived from ``records`` like :attr:`noise`, so it cannot disagree with
        the manifest beside it.
        """
        return _summarise_speckle(self.records)

    def to_dict(self) -> dict[str, Any]:
        item_ids = sorted({r.item_id for r in self.records})
        # The conversion block appears only when one ran, so an unconverted
        # run's summary is unchanged by this field existing.
        extra: dict[str, Any] = (
            {"conversion": asdict(self.conversion)} if self.conversion is not None else {}
        )
        noise = self.noise
        if noise is not None:
            extra["noise"] = noise.to_dict()
        speckle = self.speckle
        if speckle is not None:
            extra["speckle"] = speckle.to_dict()
        # Absent from a run that skipped nothing, so the payload of a dataset
        # with no hole in it is unchanged by this field existing.
        if self.skipped:
            extra["skipped_count"] = len(self.skipped)
            extra["skipped"] = [s.to_dict() for s in self.skipped]
            # Only when the sidecar was actually written: a run that collected
            # its records without a manifest has the hole in the payload and
            # nowhere on disk, and saying otherwise would point at no file.
            if self.skipped_path is not None:
                extra["skipped_manifest"] = self.skipped_path
        # Likewise absent from a run that did not preflight, so no existing
        # payload gains a field by this option existing.
        if self.preflight is not None:
            extra["preflight"] = self.preflight.to_dict()
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
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
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
    speckle_filter:
        Optionally average speckle down, one of
        :data:`umbra_py.convert.SPECKLE_FILTERS` -- so a tile teaches a model the
        surface rather than the interference pattern coherent illumination made
        on it, whose standard deviation equals its mean on a single look. It runs
        wherever it is most correct for the asset: on a **published amplitude
        raster** the tiles themselves are averaged, which is the first (and only)
        point at which those pixels exist in this library at all; on a
        ``SICD`` it is routed into the conversion
        (:attr:`SicdConversion.speckle_filter`) and runs in the radar's own image
        space before geocoding, where speckle is one independent sample per pixel.
        ``None`` (the default) filters nothing: what a window spends is
        resolution, so it is a request rather than a default.

        On the raster path each tile is read with a ``speckle_window // 2`` halo
        and cropped back after filtering, so every chip pixel averages the
        neighbours it would have had in a whole-scene filter -- which is what
        makes two overlapping tiles agree about the ground they share -- and
        ``"lee"``'s speckle parameter is read once for the acquisition
        (:func:`_scene_speckle`) rather than per tile. Both numbers land in every
        :class:`ChipRecord`.
    speckle_window:
        Edge of the odd, centred window ``speckle_filter`` averages over, in
        pixels (:data:`umbra_py.convert.SPECKLE_WINDOW_DEFAULT`). Wider removes
        more speckle and more detail; it costs no more to compute.
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
    requested = _resolve_chip_speckle(speckle_filter, speckle_window)
    complex_asset = asset.upper() in COMPLEX_ASSETS
    if bbox is not None:
        # One decision, applied in both places: a complex product is geocoded
        # only over the area of interest, and every asset is then tiled over it.
        conversion = replace(conversion or SicdConversion(), bbox=_as_bbox(bbox))
    if requested is not None and complex_asset:
        # Filtered in image space instead, before the geocoding -- so nothing is
        # left for the tile loop to do and the record comes back off the
        # converted raster's own tags like every other conversion setting.
        conversion = _with_conversion_speckle(conversion, *requested)
        requested = None
    source_cm = _chip_source(
        item, asset, conversion=conversion, work_dir=work_dir, preparer=preparer
    )
    with source_cm as source, rasterio.open(source) as src:
        nodata = src.nodata
        crs = src.crs
        crs_str = crs.to_string() if crs else None
        provenance = _provenance_tags(src)
        calibration = _reported_step(provenance, "CALIBRATION")
        noise_subtraction = _reported_step(provenance, "NOISE_SUBTRACTION")
        noise_floored_fraction = _reported_number(provenance, "NOISE_FLOORED_FRACTION")
        noise_floor_margin_db = _reported_number(provenance, "NOISE_FLOOR_MARGIN_DB")
        chip_filter = _reported_step(provenance, "SPECKLE_FILTER")
        chip_window = _reported_number(provenance, "SPECKLE_WINDOW")
        enl_before = _reported_number(provenance, "SPECKLE_ENL_BEFORE")
        enl_after = _reported_number(provenance, "SPECKLE_ENL_AFTER")
        looks = _reported_number(provenance, "SPECKLE_LOOKS")
        rtc_model = _reported_step(provenance, "RTC_MODEL")
        if bbox is None:
            row0, col0, row_stop, col_stop = 0, 0, src.height, src.width
        else:
            row0, col0, row_stop, col_stop = _clip_pixel_window(src, bbox)

        speckle: _ChipSpeckle | None = None
        if requested is not None:
            name, size = requested
            if chip_filter is not None:
                raise ValueError(
                    f"Refusing to speckle-filter tiles cut from an already-filtered raster "
                    f"({chip_filter}, {chip_window}x{chip_window} window, recorded by "
                    "'umbra convert'): averaging twice leaves a chip whose effective "
                    "resolution is neither window, so the record it would carry -- and "
                    "anything a model learns from it -- would understate what the "
                    "smoothing cost."
                )
            # Once for the acquisition, before any tile is cut, so every tile is
            # filtered with the same parameter (see `_scene_speckle`).
            scene = _scene_speckle(src, name, size, (row0, col0, row_stop, col_stop))
            speckle = _ChipSpeckle(name, size, scene)
            chip_filter, chip_window = name, float(size)
            enl_before, enl_after, looks = scene.enl_before, scene.enl_after, scene.looks
            provenance = {**provenance, **_speckle_provenance(scene)}

        rows = range(row0, row_stop - chip_size + 1, step)
        cols = range(col0, col_stop - chip_size + 1, step)
        for row, r0 in enumerate(rows):
            for col, c0 in enumerate(cols):
                window = Window(c0, r0, chip_size, chip_size)
                if speckle is None:
                    data = src.read([1], window=window)[0].astype("float32")
                else:
                    # A halo of half a window on every side, clipped to the
                    # raster: every chip pixel then averages the neighbours a
                    # whole-scene filter would have given it, which is the only
                    # way two overlapping tiles can agree about shared ground.
                    pad = speckle.window // 2
                    hr0, hc0 = max(0, r0 - pad), max(0, c0 - pad)
                    hr1 = min(src.height, r0 + chip_size + pad)
                    hc1 = min(src.width, c0 + chip_size + pad)
                    halo = src.read([1], window=Window(hc0, hr0, hc1 - hc0, hr1 - hr0))[0].astype(
                        "float32"
                    )
                    inner = (
                        slice(r0 - hr0, r0 - hr0 + chip_size),
                        slice(c0 - hc0, c0 - hc0 + chip_size),
                    )
                    data = halo[inner]

                invalid = _invalid_mask(np, data, nodata)
                valid_fraction = float(1.0 - invalid.mean())
                if valid_fraction < min_valid:
                    # Decided before the filter runs, on the values the tile was
                    # read with: a filter changes values, not the mask, so a
                    # dropped tile is the same tile either way and this only
                    # saves the work.
                    continue

                if speckle is not None:
                    from .convert import _filter_speckle  # noqa: PLC0415

                    # Excluded rather than zeroed, so an edge pixel averages the
                    # returns it has instead of being dragged toward nothing.
                    masked = np.where(_invalid_mask(np, halo, nodata), np.nan, halo)
                    filtered, _achieved = _filter_speckle(
                        masked,
                        decibels=False,
                        name=speckle.name,
                        window=speckle.window,
                        # `lee`'s speckle parameter. For `boxcar`, which needs
                        # none, any value says the other half of what passing it
                        # means: this scene's statistics were established once in
                        # `_scene_speckle` and are not to be re-read off a tile.
                        looks=speckle.scene.looks if speckle.scene.looks is not None else 1.0,
                    )
                    data = filtered[inner]

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
                        noise_subtraction=noise_subtraction,
                        noise_floored_fraction=noise_floored_fraction,
                        noise_floor_margin_db=noise_floor_margin_db,
                        speckle_filter=chip_filter,
                        # An int in the manifest: it is a pixel count, and a
                        # loader comparing it against a chip size should not have
                        # to think about 5.0 vs 5.
                        speckle_window=int(chip_window) if chip_window is not None else None,
                        speckle_enl_before=enl_before,
                        speckle_enl_after=enl_after,
                        speckle_looks=looks,
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


def write_skipped_manifest(skipped: Sequence[SkippedAcquisition], path: str | os.PathLike) -> Path:
    """Write the acquisitions a run left out to a ``.jsonl`` sidecar.

    One JSON object per skipped acquisition -- :meth:`SkippedAcquisition.to_dict`
    verbatim, so which pass is missing, when it was taken, the product's own
    words for why, the recovery hint and the ``stage`` it was found at all read
    the same from the file as from :attr:`ChipDataset.skipped`.

    It is a *sidecar* rather than rows in the manifest because the manifest's
    schema is one row per chip, and a skipped acquisition has no chip: writing it
    there would mean a record with no path, no bbox and no transform, which every
    consumer of that schema would then have to learn to ignore. A separate file
    with its own one-row-per-acquisition schema costs a loader one ``open()`` and
    costs a chip reader nothing.

    Always ``.jsonl``, whatever format the manifest beside it is. The three
    manifest formats are three ways of describing *tiles* -- footprint polygons
    for QGIS, a column-oriented table for DuckDB -- and none of them is what a
    missing acquisition is. The one thing a caller does with this file is read it
    line by line to find out what is not in the dataset.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for skip in skipped:
            fh.write(json.dumps(skip.to_dict()) + "\n")
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
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
    manifest: str | None = "manifest.jsonl",
    skipped_manifest: str | None = "skipped.jsonl",
    progress: ProgressFn | None = None,
    conversion: SicdConversion | None = None,
    work_dir: str | os.PathLike | None = None,
    preparer: SicdPreparer | None = None,
    skip_unsupported: bool = False,
    preflight: bool = False,
    preflight_progress: PreflightProgressFn | None = None,
    preflight_workers: int | None = None,
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

    ``skipped_manifest`` is the filename of the sidecar that states what the run
    could *not* include (see :func:`write_skipped_manifest`), written beside the
    manifest and **only when there is something to record** -- so a dataset with
    no hole in it is exactly the set of files it was before, and the file's
    presence is itself the statement that there is a hole. It follows
    ``manifest``: ``manifest=None`` means "collect the records, write nothing",
    and that stays true. Pass ``None`` to suppress the sidecar on its own.

    Writing it at all is the difference between a run that knows what it left
    out and a *dataset* that does. :attr:`ChipDataset.skipped` and the ``--json``
    payload describe the hole to whoever watched the run; a training loader
    reading ``out_dir`` months later sees only the files, and without the sidecar
    a dataset that dropped half its passes is indistinguishable from one that was
    only ever offered half.

    ``bbox`` restricts every acquisition to one area of interest (see
    :func:`chip_item`) -- the usual shape of a dataset build, where the site is
    the subject and the scenes are just the passes over it.

    ``speckle_filter`` / ``speckle_window`` average speckle down in every scene
    (see :func:`chip_item`), which for a complex ``asset`` means routing the
    request into ``conversion`` -- so the settings the summary reports are the
    ones that ran, whichever path they took.

    ``conversion`` / ``work_dir`` / ``preparer`` apply when ``asset`` is a
    complex product (see :func:`chip_item`). Each acquisition is prepared and
    chipped in turn, so a run over many SICDs holds one scene on disk at a time
    unless ``work_dir`` is set to keep them.

    ``skip_unsupported`` decides what a batch does when one acquisition's own
    metadata cannot support the measurement asked of it -- a product with no
    ``Radiometric`` block under ``calibration=``, no stated noise floor under
    ``noise_model="measured"``, or no stated collection geometry under ``rtc=``.
    The default raises, which is right for a run
    over one product's worth of scenes: if the archive cannot answer, the answer
    is not a smaller dataset. Over a mixed archive it is the wrong default,
    because the twenty scenes already chipped are lost to the twenty-first, so
    ``True`` records the refusal on :attr:`ChipDataset.skipped` and moves to the
    next acquisition. The dataset then *states* its hole rather than having one:
    which passes are missing, and in each product's own words why.

    Only :class:`~umbra_py.exceptions.UnsupportedMeasurementError` is skipped.
    Everything else -- a download failure, a missing asset, a corrupt product --
    still ends the run, because a batch that swallows unknown errors is a batch
    whose output nobody can trust.

    ``preflight`` asks the same question *before* any product is downloaded.
    ``skip_unsupported`` makes a refusal survivable, but it is still discovered
    by attempting the conversion, and for a complex asset that means the whole
    multi-gigabyte NITF is fetched to learn that its metadata cannot answer --
    twenty times over a site's twenty passes. With ``preflight=True`` each
    acquisition's SICD XML is read over the wire instead (see
    :mod:`umbra_py.preflight`: a NITF states its own layout, so two range
    requests and a few tens of kilobytes locate and fetch it), the conversion's
    own support check is run against it, and the passes that positively cannot
    answer never reach the download at all. They are recorded on
    :attr:`ChipDataset.skipped` exactly as a survived refusal is -- because a
    dataset with a hole in it has to say so however cheaply the hole was found --
    with ``stage="preflight"`` and a :class:`PreflightSummary` roll-up saying
    what the check cost and what it saved.

    An acquisition whose metadata read fails *on the wire* is **kept**: a
    transport failure is not a product declaring anything, so the run proceeds to
    find out the expensive way rather than dropping a scene over a blip. One
    whose read fails on the *product* -- the item lists no such asset, nothing is
    at the href, what is there is not a NITF or carries no SICD XML -- is dropped
    like a refusal, because that is what it is. Keeping those was never the
    cautious half of the choice: such a pass fails inside :func:`chip_item` as a
    plain read error, which ``skip_unsupported`` deliberately does not catch, so
    the run ends on an acquisition its own preflight had already ruled out. The
    two are counted apart on :class:`PreflightSummary` (``missing`` against
    ``unreadable``) so the summary says which kind of hole a dataset has.

    ``preflight`` is only meaningful for a complex ``asset`` -- a GEC or CSI
    carries no SICD metadata to ask -- and asking for it on a raster asset is
    refused rather than quietly ignored. It composes with ``skip_unsupported``,
    which stays worth passing: the preflight only asks the two questions the
    metadata answers (calibration and a measured noise floor), so a refusal from
    anywhere else still arrives at conversion time.

    ``preflight_workers`` is how many of those metadata reads run at once
    (``None`` takes :data:`umbra_py.preflight.DEFAULT_PREFLIGHT_WORKERS`). The
    check costs round trips rather than bytes, so a serial one puts a stall in
    front of the batch that grows with the number of passes -- which is the one
    cost the preflight would otherwise have added to a run over a large site.
    """
    out_path = Path(out_dir)
    items = list(items)
    requested = _resolve_chip_speckle(speckle_filter, speckle_window)
    # Resolve the defaults here rather than per item, so the dataset summary
    # reports the settings that actually ran even when the caller passed none.
    if asset.upper() in COMPLEX_ASSETS:
        conversion = conversion or SicdConversion()
        if bbox is not None:
            conversion = replace(conversion, bbox=_as_bbox(bbox))
        if requested is not None:
            conversion = _with_conversion_speckle(conversion, *requested)
            requested = None
    else:
        conversion = None
    records: list[ChipRecord] = []
    skipped: list[SkippedAcquisition] = []
    preflight_summary: PreflightSummary | None = None
    if preflight:
        items, dropped, preflight_summary = _preflight_filter(
            items,
            asset=asset,
            conversion=conversion,
            progress=preflight_progress,
            workers=preflight_workers,
        )
        skipped.extend(dropped)
    for i, item in enumerate(items):
        try:
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
                speckle_filter=None if requested is None else requested[0],
                speckle_window=speckle_window if requested is None else requested[1],
                conversion=conversion,
                work_dir=work_dir,
                preparer=preparer,
            )
        except UnsupportedMeasurementError as exc:
            if not skip_unsupported:
                raise
            # Only this one type, and only when asked: it is the refusal that is
            # a fact about the product rather than about the run, so the next
            # acquisition can still be the answer.
            skipped.append(
                SkippedAcquisition(
                    item_id=item.id,
                    reason=str(exc),
                    # The same ISO string a ChipRecord carries, so a skipped
                    # pass and a chipped one are comparable in the same payload.
                    datetime=item.datetime.isoformat() if item.datetime else None,
                    hint=exc.hint,
                )
            )
            recs = []
        records.extend(recs)
        if progress is not None:
            progress(i + 1, len(items), item, len(recs))

    manifest_path: str | None = None
    skipped_path: str | None = None
    if manifest is not None:
        written = write_manifest(records, out_path / manifest)
        manifest_path = str(written)
        # Absent from a run that skipped nothing, for the same reason the
        # `skipped` block is absent from that run's payload: a file that says
        # "no holes" and a missing file mean the same thing, and only one of
        # them changes what a clean run leaves on disk.
        if skipped and skipped_manifest is not None:
            skipped_path = str(write_skipped_manifest(skipped, out_path / skipped_manifest))

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
        skipped=tuple(skipped),
        preflight=preflight_summary,
        skipped_path=skipped_path,
    )


def _preflight_filter(
    items: list[UmbraItem],
    *,
    asset: str,
    conversion: SicdConversion | None,
    progress: PreflightProgressFn | None = None,
    workers: int | None = None,
) -> tuple[list[UmbraItem], list[SkippedAcquisition], PreflightSummary]:
    """Drop the acquisitions that declare they cannot answer, before downloading any.

    Reads each product's metadata by range request and applies the conversion's
    own support check (see :mod:`umbra_py.preflight`), returning the passes to
    keep, the ones dropped as :class:`SkippedAcquisition` records, and the
    roll-up of what asking cost.

    The settings asked about come from ``conversion`` rather than from separate
    parameters, so the question the preflight asks is by construction the one the
    conversion will ask: a pass this clears cannot then be refused for a reason
    this could have seen.
    """
    from .preflight import preflight_items  # noqa: PLC0415 - keeps `requests` off the import path

    if asset.upper() not in COMPLEX_ASSETS or conversion is None:
        raise ValueError(
            f"preflight=True needs a complex asset ({', '.join(COMPLEX_ASSETS)}); "
            f"{asset!r} is an amplitude raster, which carries no SICD metadata to "
            "ask. Drop the flag, or chip the complex product."
        )
    report = preflight_items(
        items,
        asset=asset.upper(),
        calibration=conversion.calibration,
        noise_subtract=conversion.noise_subtract,
        noise_model=conversion.noise_model,
        rtc=conversion.rtc,
        progress=progress,
        workers=workers,
    )
    kept: list[UmbraItem] = []
    dropped: list[SkippedAcquisition] = []
    unreadable = 0
    missing = 0
    saved: list[int] = []
    # `preflight_items` returns one result per item in order, so pairing them is
    # exact -- no lookup by id, which two passes of one task could collide on.
    for item, result in zip(items, report.results, strict=True):
        if not result.final:
            # The read failed on the wire. That is a fact about the moment, not
            # about the product, so it does not get to remove a scene from the
            # dataset: the run keeps the pass and finds out the expensive way.
            unreadable += 1
            kept.append(item)
            continue
        if result.supported:
            kept.append(item)
            continue
        if result.capabilities is None:
            # No readable product behind this acquisition -- nothing at the href,
            # or something that is not a SICD. Dropping it is not the cautious
            # branch's opposite but its point: keeping it only defers the same
            # failure to `chip_item`, where it is a plain read error rather than
            # an `UnsupportedMeasurementError`, so `--skip-unsupported` does not
            # catch it and the whole run dies on a pass the preflight had
            # already decided.
            missing += 1
        dropped.append(
            SkippedAcquisition(
                item_id=item.id,
                reason=result.reason
                or result.error
                or "the product's metadata cannot support the request",
                datetime=item.datetime.isoformat() if item.datetime else None,
                hint=result.hint,
                stage="preflight",
            )
        )
        if result.capabilities and result.capabilities.product_bytes:
            saved.append(result.capabilities.product_bytes)
    return (
        kept,
        dropped,
        PreflightSummary(
            checked=len(report.results),
            supported=len(kept) - unreadable,
            skipped=len(dropped),
            unreadable=unreadable,
            missing=missing,
            bytes_read=report.bytes_read,
            # Only the dropped products are a saving: a supported one is
            # downloaded anyway, so its header read is overhead rather than
            # something avoided.
            product_bytes_skipped=sum(saved) if saved else None,
        ),
    )
