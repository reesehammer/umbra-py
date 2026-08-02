"""Analysis-ready loading of Umbra SAR imagery into ``xarray`` (the *load* step).

The project tagline is "discover, **load**, download, and analyze". Discovery,
download and pretty-picture visualization already exist; this module fills the
missing verb: turning a geocoded Umbra GeoTIFF into a georeferenced
:class:`xarray.DataArray` so the data drops straight into the scientific Python
stack (``xarray``/``dask``/``matplotlib``/``scikit-image``/``rioxarray`` ...).

Why this matters for adoption: every Sentinel-1 / Landsat workflow starts by
loading a scene into a labelled, georeferenced array. Until now an Umbra user
had to hand-roll ``rasterio`` + windowing + coordinate construction to get
there. :func:`to_xarray` makes it one call -- and because the source is a
cloud-optimized GeoTIFF read through GDAL's ``/vsicurl/`` driver, only the bytes
for the requested window and resolution are streamed over HTTP range requests
(no multi-gigabyte download).

:func:`to_stack` is the *time series* half of the same verb. Elsewhere in the
STAC ecosystem that step is ``stackstac`` / ``odc-stac``: search returns N
scenes, and one call co-registers them onto a shared grid and hands back a
``(time, y, x)`` datacube. Neither works against Umbra, because both assume a
STAC *API* and a common projected grid, and Umbra's passes over one site land
in whatever UTM zone (and at whatever extent) each acquisition happened to use.
:func:`to_stack` does the warp-to-a-common-grid step itself, so a search result
becomes a labelled cube you can take ``.mean("time")``, ``.diff("time")`` or
``.std("time")`` of -- the primitive every multi-date SAR analysis starts from,
and the one thing the library previously only produced as a *picture*
(``umbra change`` / ``umbra timescan``) rather than as numbers.

A cube's size is ``max_size²`` × the number of passes, so a long series used to
have to be stacked coarse to fit in memory. ``to_stack(lazy=True)`` removes that
trade: each pass becomes one ``dask`` task (and one chunk), fetched only when
something asks for its values, and the reductions that consume a cube --
:func:`stack_stats`, :func:`stack_to_geotiff` -- walk it a slice at a time. The
numbers are identical; only the peak memory differs. ``chunk_size=`` takes the
same step within a pass: a slice is cut into windows read independently, so the
unit of work stops being a whole ``max_size²`` slab and one scene no longer has
to fit in memory either. ``stack_stats(windowed=True)`` finishes the chain by
*measuring* those windows rather than whole passes, at the cost of the one
statistic a window cannot carry exactly: a percentile.

``to_stack(speckle_filter=…)`` averages **speckle** down on the shared grid
before the series is assembled. That correction previously reached only the
complex archive (``umbra convert --speckle-filter``, which filters in the
radar's own image space), so the products this library actually exists for --
Umbra's published GEC rasters, already geocoded and never converted here --
carried the dominant uncertainty in every number this module reports, with
nothing able to remove it. Filtering here is the same power-domain averaging one
step further down the chain: it is what makes a *cube* a less noisy measurement,
rather than what makes its inputs one. It composes with ``chunk_size``, so the
sharpest cube this module can build is also the least noisy one it can measure:
each window is read with a half-window halo and cropped after filtering, and
``"lee"``'s speckle parameter -- a property of the pass, not of one window -- is
resolved once per acquisition.

Install with: ``pip install "umbra-py[load]"`` (add ``[dask]`` for lazy cubes).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .constants import ATTRIBUTION, DATA_LICENSE
from .convert import SPECKLE_FILTERS, SPECKLE_WINDOW_DEFAULT
from .exceptions import AssetNotFoundError, MissingDependencyError
from .models import UmbraItem

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from collections.abc import Iterable

    import xarray as xr

#: Geographic bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
BBox = tuple[float, float, float, float]

#: How :func:`to_stack` picks the datacube's footprint from the scenes it stacks.
#: ``"intersection"`` keeps only the ground every acquisition covers (every cell
#: has a full time series -- what change detection wants); ``"union"`` keeps all
#: ground any acquisition covers, filling each slice outside its own footprint
#: with ``NaN``.
STACK_EXTENTS = ("intersection", "union")

#: The value of :func:`to_stack`'s ``crs=`` that asks for the UTM zone the
#: stacked ground falls in, rather than a CRS named outright.
STACK_AUTO_CRS = "utm"

#: Bin width of the histogram :func:`stack_stats`'s ``windowed=True`` mode
#: estimates medians and percentiles from, in decibels. Those are the only
#: numbers that mode reports approximately, and this is the scale of the error:
#: about a bin -- 0.05 dB, or 0.6 % of amplitude -- against a measurement the
#: caveats already call relative rather than absolute.
_QUANTILE_BIN_DB = 0.05

#: The :func:`umbra_py.convert.conversion_tags` keys that decide what a pixel
#: value *is*, and so which ones :func:`to_stack` refuses to mix. A stack's time
#: axis is only a measurement if every slice was made the same way: differencing
#: a calibrated pass against an uncalibrated one, or a terrain-flattened pass
#: against a raw one, reports the difference between the two *conversions* as
#: change on the ground. ``speckle_filter`` / ``speckle_window`` are here for the
#: same reason one step further out: a speckle-filtered pass reports the average
#: of the ground a window covers, so differencing it against an unfiltered pass
#: (or one filtered with a different window) reports the smoothing as change,
#: strongest exactly where the ground has the most structure. The keys
#: deliberately left out are the ones that legitimately vary per acquisition --
#: ``source`` (a different scene each time), ``rtc_reference_deg`` (each scene's
#: own resolved incidence angle) and the per-scene diagnostics of the noise
#: subtraction and the speckle filter (how much of *that* scene hit the sensor's
#: floor, the ENL *that* scene reached).
#: Ordered most-explanatory first, because the first key that disagrees is the
#: one the refusal names, and ``units`` is derived from the two before it -- a
#: calibration mix should be reported as a calibration mix, not as its shadow.
MEASUREMENT_PROVENANCE_KEYS = (
    "calibration",
    "noise_subtraction",
    "speckle_filter",
    "speckle_window",
    "rtc_model",
    "scale",
    "units",
)

#: Stands in for "this raster carries no umbra-py provenance at all" when
#: comparing sources. A published Umbra GEC has no ``UMBRA_*`` tags, so a whole
#: series of them agrees on this value and nothing is refused; it is the *mix* of
#: a converted raster with an untagged one that the sentinel catches.
_UNRECORDED = "(unrecorded)"

#: What a *converted* raster that is silent about a step is taken to have done:
#: nothing. ``conversion_tags`` writes ``"none"`` for every step that did not
#: run, so the only way a key goes missing from a real record is a raster
#: converted by an older umbra-py that had no such step -- which did not run it
#: either. Reading that as ``"none"`` rather than :data:`_UNRECORDED` keeps a
#: new key from retroactively splitting a series that agrees; a raster with *no*
#: umbra-py provenance is still :data:`_UNRECORDED`, because there the silence
#: is about the whole conversion rather than one step of it.
_STEP_NOT_RUN = "none"


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise MissingDependencyError(
            f"'{module}' is required for analysis-ready loading. "
            'Install the extra with: pip install "umbra-py[load]"',
            hint='pip install "umbra-py[load]"',
        ) from exc


def _require_dask():
    """``(dask, dask.array)`` for the lazy stacking path, or a pointed error.

    Its own gate rather than :func:`_require`'s: ``dask`` is a *separate*,
    heavier extra from ``[load]`` (it brings a task scheduler), so a missing
    install here means "you asked for the chunked cube", not "loading is not
    installed", and the hint has to name the extra that fixes it.
    """
    try:
        import dask  # noqa: PLC0415
        import dask.array as dask_array  # noqa: PLC0415
    except ImportError as exc:
        raise MissingDependencyError(
            "'dask' is required for lazy (chunked) stacking. "
            'Install the extra with: pip install "umbra-py[dask]"',
            hint='pip install "umbra-py[dask]"',
        ) from exc
    return dask, dask_array


def _open_path(url: str) -> str:
    """Path to hand ``rasterio.open``: stream remote COGs, open local files directly.

    Umbra's public assets are ``https`` cloud-optimized GeoTIFFs, which GDAL
    reads with range requests via the ``/vsicurl/`` driver. A plain local path
    (used in tests, or for an already-downloaded file) is opened as-is.
    """
    if url.startswith(("http://", "https://")):
        return f"/vsicurl/{url}"
    return url


def to_xarray(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int | None = None,
    db: bool = False,
    masked: bool = True,
) -> xr.DataArray:
    """Load an Umbra SAR image as a georeferenced :class:`xarray.DataArray`.

    Reads band 1 of the item's geocoded GeoTIFF (the ``GEC`` cloud-optimized
    GeoTIFF by default) and returns it as a 2D ``DataArray`` with ``y`` / ``x``
    coordinate axes in the raster's native CRS, ready for the scientific Python
    stack. Only the requested window and resolution are streamed via HTTP range
    requests -- no full download.

    Parameters
    ----------
    item:
        The acquisition to load.
    asset:
        Which product to read. Defaults to ``"GEC"``; ``"CSI"`` (the
        single-band color-sub-aperture GeoTIFF) also works. The complex
        ``SICD`` / ``CPHD`` products are not amplitude rasters and aren't
        supported here.
    bbox:
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
        given, only that geographic window is read (reprojected to the
        raster's CRS first). Useful for pulling a small area out of a large
        scene without reading the whole thing.
    max_size:
        Optional cap on the longest output side in pixels. The raster is
        decimated to fit (GDAL pulls the matching cloud-optimized GeoTIFF
        overview, so this is cheap). ``None`` reads full resolution -- which
        for a multi-GB scene can be a lot of data; pair large reads with a
        ``bbox`` or a ``max_size``.
    db:
        Convert linear amplitude to decibels (``20*log10(amplitude)``), the
        radiometrically-meaningful SAR scale. Implies ``masked=True`` for the
        non-positive pixels ``log10`` can't represent.
    masked:
        Replace nodata and non-positive amplitudes with ``NaN`` so they don't
        contaminate statistics. The array is always returned as ``float32``.

    Returns
    -------
    xarray.DataArray
        Dimensions ``("y", "x")`` with descending ``y`` (north-up) and
        ascending ``x`` cell-center coordinates. ``attrs`` carry the CRS
        (``crs``, a WKT/PROJ string), the affine ``transform`` (a 6-tuple),
        the geographic ``bounds``, ``units``, and acquisition metadata
        (``item_id``, ``datetime``, ``platform``, ``product_type``), plus the
        Umbra ``license`` and ``attribution`` you must carry with derived
        products. The CRS string round-trips through ``rasterio.crs.CRS`` /
        ``pyproj`` and ``rioxarray`` (``da.rio.write_crs(da.attrs["crs"])``).

        A raster ``umbra convert`` produced also carries a ``provenance`` dict
        -- exactly what :func:`~umbra_py.convert.read_conversion_tags` reads off
        the file (``calibration``, ``rtc_model``, ``scale``, ``dem``, ...) -- so
        an array knows whether its values are a physical backscatter coefficient
        or relative brightness. The key is absent for Umbra's published products,
        which carry no such tags.
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    xr = _require("xarray")
    from affine import Affine  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.windows import Window, from_bounds  # noqa: PLC0415

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(
            f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
            "(asset href is empty and no umbra:task_id available to derive one)."
        )

    with rasterio.open(_open_path(url)) as src:
        # Restrict to the requested geographic window (in the source CRS).
        if bbox is not None:
            from rasterio.errors import WindowError  # noqa: PLC0415
            from rasterio.warp import transform_bounds  # noqa: PLC0415

            left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox)
            requested = from_bounds(left, bottom, right, top, transform=src.transform)
            try:
                window = requested.intersection(Window(0, 0, src.width, src.height))
            except WindowError:
                window = None
            if window is None or window.width < 1 or window.height < 1:
                raise ValueError(
                    f"bbox {bbox} does not overlap item {item.id!r} "
                    f"(bounds {tuple(src.bounds)} in {src.crs})."
                )
        else:
            window = Window(0, 0, src.width, src.height)

        # Decimate to fit max_size (GDAL serves the matching COG overview).
        scale = 1.0
        if max_size is not None:
            scale = max(max(window.width, window.height) / max_size, 1.0)
        out_w = max(round(window.width / scale), 1)
        out_h = max(round(window.height / scale), 1)

        # Read band 1 via a list index into a 3-D out_shape and drop the band
        # axis ourselves. Rasterio's scalar-index + 2-D out_shape path squeezes
        # the result in place with an ndarray.shape assignment, which NumPy 2.5
        # deprecates; a list index returns a 3-D array with no in-place reshape.
        data = src.read(
            [1],
            window=window,
            out_shape=(1, out_h, out_w),
            resampling=Resampling.average,
        )[0].astype("float32")

        nodata = src.nodata
        crs = src.crs
        # Read while the dataset is open: for a raster ``umbra convert`` wrote,
        # this is what the pixel values *are* (calibration, RTC model, scale).
        provenance = _source_provenance(src)
        # Output transform: window origin scaled to the decimated grid. GEC is
        # north-up (no rotation), so a/e fully describe pixel size and y runs
        # top-to-bottom (negative e).
        win_transform = src.window_transform(window)
        transform = win_transform * Affine.scale(window.width / out_w, window.height / out_h)

    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    if db:
        masked = True
        invalid |= data <= 0
        with np.errstate(divide="ignore", invalid="ignore"):
            data = np.where(invalid, np.nan, 20.0 * np.log10(data)).astype("float32")
    elif masked:
        invalid |= data <= 0
        data = np.where(invalid, np.nan, data).astype("float32")

    # Cell-center coordinates from the affine transform (b == d == 0, north-up).
    xs = transform.c + transform.a * (np.arange(out_w) + 0.5)
    ys = transform.f + transform.e * (np.arange(out_h) + 0.5)
    left, top = transform.c, transform.f
    right, bottom = transform * (out_w, out_h)

    dt = item.datetime
    attrs: dict[str, Any] = {
        "crs": crs.to_string() if crs else None,
        "transform": tuple(transform)[:6],
        "bounds": (left, bottom, right, top),
        "units": "dB" if db else "amplitude",
        "long_name": "SAR backscatter (dB)" if db else "SAR amplitude",
        "item_id": item.id,
        "datetime": dt.isoformat() if dt else None,
        "platform": item.platform,
        "product_type": asset,
        "license": DATA_LICENSE,
        "attribution": ATTRIBUTION,
        # Only for a raster that carries one; an Umbra-published product does
        # not, and an empty record would read as "nothing was done" rather than
        # "nothing is known".
        "provenance": provenance or None,
    }

    return xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"y": ys, "x": xs},
        name="backscatter_db" if db else "amplitude",
        attrs={k: v for k, v in attrs.items() if v is not None},
    )


def to_geotiff(
    item: UmbraItem,
    dest: str | os.PathLike,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int | None = None,
    db: bool = False,
) -> Path:
    """Load an Umbra SAR image and write it to ``dest`` as a GeoTIFF.

    A file-producing companion to :func:`to_xarray` for users who want a
    clipped / decimated raster on disk (for QGIS, GDAL, or any GIS) rather
    than an in-memory array. Same windowing and resolution options: ``bbox``
    clips to a lon/lat area, ``max_size`` decimates via the cloud-optimized
    GeoTIFF overviews, ``db`` writes the decibel scale. Only the requested
    window/resolution is streamed (no full download).

    The output is a single-band ``float32`` GeoTIFF in the source raster's
    native CRS, with nodata / non-positive pixels written as ``NaN``
    (``nodata=NaN``) so masking survives the round-trip. Deflate-compressed
    and tiled.
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from affine import Affine  # noqa: PLC0415

    da = to_xarray(item, asset=asset, bbox=bbox, max_size=max_size, db=db, masked=True)
    data = da.values

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": da.attrs.get("crs"),
        "transform": Affine(*da.attrs["transform"]),
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(dest, "w", **profile) as dst:
        dst.write(data, 1)
        dst.update_tags(
            item_id=item.id,
            units=da.attrs["units"],
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
            # A conversion's provenance survives being loaded and written back
            # out, so the derivative answers 'what is a pixel here?' too.
            **_as_geotiff_tags(da.attrs.get("provenance") or {}),
        )
    return dest


def _source_provenance(ds: Any) -> dict[str, str]:
    """The ``UMBRA_*`` conversion provenance of an open source raster.

    Empty for anything umbra-py did not convert, which is every product Umbra
    publishes -- the tags exist only on the output of ``umbra convert`` (and of
    the chipper and the writers below, which carry them forward).
    """
    from .convert import conversion_provenance  # noqa: PLC0415

    return conversion_provenance(ds.tags())


def _as_geotiff_tags(provenance: dict[str, str]) -> dict[str, str]:
    """Re-namespace a carried provenance record as GeoTIFF tags for a derivative.

    The inverse of :func:`~umbra_py.convert.conversion_provenance`, so a raster
    written *from* a cube says what its values are in the same vocabulary the
    raster the cube was read from used -- ``read_conversion_tags`` and
    ``gdalinfo`` answer the same question of both.
    """
    from .convert import PROVENANCE_TAG_PREFIX  # noqa: PLC0415

    return {f"{PROVENANCE_TAG_PREFIX}{key.upper()}": value for key, value in provenance.items()}


def _comparable_record(record: dict[str, str]) -> dict[str, str]:
    """A source's measurement keys, filled in the way the refusal compares them.

    One function so the refusal (:func:`_shared_provenance`) and the *preflight*
    that reports a selection's mix ahead of it (:func:`stack_provenance`) group
    rasters by the same rule -- a preflight that says a series agrees must not be
    contradicted by the stack it cleared, which is the same construction
    :mod:`umbra_py.preflight` uses to keep its verdict the conversion's own.

    The two absences are different claims, so they get different values: a raster
    with *no* umbra-py provenance is :data:`_UNRECORDED` (nothing is known about
    how it was made), while a converted raster that is merely silent about one
    step did not run it, :data:`_STEP_NOT_RUN`.
    """
    missing = _STEP_NOT_RUN if record else _UNRECORDED
    return {key: record.get(key, missing) for key in MEASUREMENT_PROVENANCE_KEYS}


def _shared_provenance(
    records: list[dict[str, str]], ids: list[str], *, action: str = "stack"
) -> dict[str, str]:
    """The provenance a measurement's sources agree on, or a refusal naming the mix.

    The consuming half of the provenance ``umbra convert`` writes. Two rasters
    converted with different settings are pixel-for-pixel indistinguishable, so
    before these tags existed a stack could silently put the difference between
    two *conversions* on the time axis and report it as change on the ground.
    Now the sources say what they are, and this is the check that reads them:
    the same "a mixed selection is not a measurement" rule ``POST
    /artifacts/stats`` already applies to polarization, applied to what the
    pixel values mean.

    Only :data:`MEASUREMENT_PROVENANCE_KEYS` are grounds for refusal. What comes
    *back* is every key on which all the sources agree, so a cube built from one
    conversion carries that conversion's whole record (and one built from
    untagged products carries nothing, the usual case). A converted raster that
    is silent about one key reads as :data:`_STEP_NOT_RUN` for it rather than as
    :data:`_UNRECORDED`, so a step added in a later umbra-py does not split a
    series whose older members simply never had it.

    ``action`` names what is being refused, because the rule outgrew the
    datacube: :func:`umbra_py.narrate.render_change_png` applies the same check
    to the pair of passes it quotes decibels between. Only the verb differs --
    why a mix is not a measurement is identical either way.
    """
    comparable = [_comparable_record(record) for record in records]
    for key in MEASUREMENT_PROVENANCE_KEYS:
        seen: dict[str, str] = {}
        for record, item_id in zip(comparable, ids, strict=True):
            seen.setdefault(record[key], item_id)
        if len(seen) > 1:
            listed = ", ".join(f"{value!r} ({item_id})" for value, item_id in sorted(seen.items()))
            raise ValueError(
                f"Refusing to {action} rasters whose {key} disagrees ({listed}): pixel "
                "values made by different conversions are not comparable along a time "
                "axis, so a change between two passes would be partly the difference "
                f"between the two conversions. ({_UNRECORDED} is a raster with no "
                "umbra-py conversion provenance, such as a published GEC.) Re-convert "
                "the series with one set of 'umbra convert' settings, or use only "
                "the acquisitions that share one -- 'umbra convert --provenance FILE' "
                "(umbra_py.read_conversion_tags) says what each raster carries."
            )

    if not any(records):
        return {}
    shared = set(records[0]).intersection(*(set(r) for r in records[1:]))
    return {
        key: records[0][key]
        for key in sorted(shared)
        if all(r[key] == records[0][key] for r in records)
    }


def _stack_items(items: Iterable[UmbraItem]) -> list[UmbraItem]:
    """Order the stack's acquisitions oldest-first, rejecting undated ones.

    The time axis *is* the point of a datacube, so an acquisition with no
    ``datetime`` can't take a position on it. Fail by name rather than dropping
    it silently or parking it at ``NaT``.
    """
    ordered = list(items)
    if not ordered:
        raise ValueError("to_stack() needs at least one acquisition.")
    undated = [i.id for i in ordered if i.datetime is None]
    if undated:
        raise ValueError(
            "Cannot stack acquisitions with no datetime (the time axis needs one): "
            + ", ".join(undated)
        )
    return sorted(ordered, key=lambda i: i.datetime)  # type: ignore[arg-type,return-value]


@dataclass(frozen=True)
class ProvenanceGroup:
    """The acquisitions of a selection whose sources agree on what a pixel *is*.

    A group is what :func:`to_stack` would accept on its own: every member's
    raster carries the same :data:`MEASUREMENT_PROVENANCE_KEYS`, so differencing
    them along a time axis measures the ground rather than the difference
    between two conversions. ``hrefs`` is the actionable half -- the item JSON
    URLs ``umbra stack`` takes as arguments, so a mixed selection's largest
    group is a command rather than a diagnosis.
    """

    #: Every measurement key, filled by :func:`_comparable_record` -- so a group
    #: of published GECs reads ``"(unrecorded)"`` throughout rather than as gaps.
    record: dict[str, str]
    item_ids: tuple[str, ...]
    #: The item JSON URL of each member, ``None`` where the item did not carry
    #: one (an item built from a dict rather than fetched from the catalog).
    hrefs: tuple[str | None, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, for ``umbra stack --provenance --json``."""
        return {
            "record": dict(self.record),
            "count": len(self.item_ids),
            "item_ids": list(self.item_ids),
            "hrefs": [href for href in self.hrefs if href],
        }


@dataclass(frozen=True)
class UnreadableSource:
    """An acquisition whose raster could not be read, so it has no verdict.

    Distinct from a group of one: a source that could not be opened says nothing
    about what its pixels are, which is not the same as saying they are
    unrecorded. It is reported apart for the reason
    :class:`umbra_py.preflight.PreflightResult` separates the two -- one is news
    about the archive, the other about the attempt.
    """

    item_id: str
    href: str | None
    error: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, for ``umbra stack --provenance --json``."""
        return {"item_id": self.item_id, "href": self.href, "error": self.error}


@dataclass(frozen=True)
class StackProvenance:
    """What a selection's sources say their pixel values are, before stacking.

    The preflight half of the refusal in :func:`_shared_provenance`. That check
    exists because two rasters converted with different settings are
    pixel-for-pixel indistinguishable, so a cube could otherwise put the
    difference between two *conversions* on the time axis and report it as change
    on the ground -- but it was only ever discoverable by hitting it, and the
    refusal's advice ("use only the acquisitions that share one") named a subset
    it could not identify. This says which acquisitions those are.

    What it costs is the opens :func:`to_stack` performs anyway -- one COG header
    per acquisition, a range request of kilobytes -- and what it saves is
    everything after them: the shared grid, the warp, and every decimated pixel
    read. It is the same move ``umbra preflight`` makes for a chip run, one layer
    up: ask the cheap question first, and ask it with the *same function* that
    would refuse, so a cleared selection cannot be turned away by the stack.
    """

    asset: str
    #: Largest group first; ties keep the selection's own (oldest-first) order.
    groups: tuple[ProvenanceGroup, ...]
    unreadable: tuple[UnreadableSource, ...]
    #: What every readable source agrees on, exactly as ``to_stack`` would carry
    #: it into ``attrs["provenance"]``. Empty when they disagree, and also for
    #: the ordinary case of a series of untagged published products.
    shared: dict[str, str]
    #: :func:`to_stack`'s own refusal text, or ``None`` when the series stacks.
    refusal: str | None

    @property
    def agrees(self) -> bool:
        """Whether :func:`to_stack` would accept this selection's provenance.

        Only the provenance: a series can agree here and still fail to stack for
        an unrelated reason (a footprint that does not overlap, a source that
        could not be read -- see :attr:`unreadable`).
        """
        return self.refusal is None

    @property
    def largest(self) -> ProvenanceGroup | None:
        """The biggest set of acquisitions that *do* agree, or ``None`` if empty.

        On a mixed selection this is the answer the refusal asks for: re-run with
        these and the stack is a measurement.
        """
        return self.groups[0] if self.groups else None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, for ``umbra stack --provenance --json``."""
        return {
            "asset": self.asset,
            "agrees": self.agrees,
            "groups": [group.to_dict() for group in self.groups],
            "unreadable": [source.to_dict() for source in self.unreadable],
            **({"shared": dict(self.shared)} if self.shared else {}),
            **({"refusal": self.refusal} if self.refusal else {}),
        }


def stack_provenance(
    items: Iterable[UmbraItem], *, asset: str = "GEC", action: str = "stack"
) -> StackProvenance:
    """Group a selection by what its sources say their pixel values are.

    Reads each acquisition's ``UMBRA_*`` conversion record -- the tags ``umbra
    convert`` stamps -- straight from its raster's header, groups the selection
    by :data:`MEASUREMENT_PROVENANCE_KEYS`, and reports whether
    :func:`to_stack` would accept it. Nothing is warped and no pixels are read,
    so a 40-pass site costs 40 header range requests rather than a cube.

    The verdict is :func:`_shared_provenance`'s own, called on the same records:
    :attr:`StackProvenance.refusal` is verbatim the ``ValueError`` a stack of
    this selection would raise, and :attr:`StackProvenance.shared` is verbatim
    the record it would carry. A preflight cannot become a second opinion about
    what stacks.

    An acquisition whose raster cannot be opened -- no such asset, nothing behind
    the href, a source that is not a readable raster -- is reported on
    :attr:`StackProvenance.unreadable` rather than grouped, because a failed read
    is not a statement about a product's pixels. It does not make the selection
    disagree; it makes the answer incomplete, and the report says so.

    Args:
        items: The acquisitions a stack would be built from.
        asset: Which product's raster to read, matching ``to_stack(asset=…)``.
        action: The verb the refusal names, matching ``_shared_provenance``.

    Returns:
        A :class:`StackProvenance` report.

    Raises:
        ValueError: If the selection is empty or an acquisition has no
            ``datetime`` -- the same two refusals :func:`to_stack` opens with,
            since a preflight of a selection that cannot be a time series at all
            would be answering the wrong question.

    Example:
        >>> report = stack_provenance(items)  # doctest: +SKIP
        >>> if not report.agrees:  # doctest: +SKIP
        ...     print(report.refusal)
        ...     print("largest agreeing subset:", report.largest.item_ids)
    """
    rasterio = _require("rasterio")
    ordered = _stack_items(items)

    records: list[dict[str, str]] = []
    read: list[UmbraItem] = []
    unreadable: list[UnreadableSource] = []
    for item in ordered:
        try:
            url = item.asset_href(asset)
        except AssetNotFoundError as exc:
            # The item does not list the product at all -- final, and the same
            # news as an href with nothing behind it, so it lands in one place.
            unreadable.append(UnreadableSource(item.id, None, str(exc)))
            continue
        if not url:
            unreadable.append(
                UnreadableSource(
                    item.id,
                    None,
                    f"Item {item.id!r} has no resolvable URL for asset {asset!r}.",
                )
            )
            continue
        try:
            with rasterio.open(_open_path(url)) as ds:
                record = _source_provenance(ds)
        except Exception as exc:  # noqa: BLE001 - any read failure is one verdict
            unreadable.append(UnreadableSource(item.id, url, f"{type(exc).__name__}: {exc}"))
            continue
        records.append(record)
        read.append(item)

    grouped: dict[tuple[str, ...], list[UmbraItem]] = {}
    keyed: dict[tuple[str, ...], dict[str, str]] = {}
    for record, item in zip(records, read, strict=True):
        comparable = _comparable_record(record)
        key = tuple(comparable[name] for name in MEASUREMENT_PROVENANCE_KEYS)
        grouped.setdefault(key, []).append(item)
        keyed.setdefault(key, comparable)
    # Stable, so a tie between two equal-sized groups keeps the oldest-first
    # order the selection arrived in rather than an arbitrary one.
    groups = tuple(
        ProvenanceGroup(
            record=keyed[key],
            item_ids=tuple(i.id for i in members),
            hrefs=tuple(i.href for i in members),
        )
        for key, members in sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    )

    refusal: str | None = None
    shared: dict[str, str] = {}
    try:
        shared = _shared_provenance(records, [i.id for i in read], action=action)
    except ValueError as exc:
        refusal = str(exc)

    return StackProvenance(
        asset=asset,
        groups=groups,
        unreadable=tuple(unreadable),
        shared=shared,
        refusal=refusal,
    )


def _utm_epsg(lon: float, lat: float) -> str:
    """EPSG code of the standard UTM zone containing ``(lon, lat)``.

    Zones are 6 degrees wide starting at -180; north and south share a zone
    number and differ only in the EPSG bank (``326xx`` / ``327xx``). The
    Norway/Svalbard exceptions are deliberately not modelled -- they widen a
    neighbouring zone, and the cube is metric either way; name the CRS outright
    if you need the local convention.
    """
    zone = int((lon + 180.0) // 6.0) % 60 + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def _resolve_stack_crs(crs: str | None, datasets: list[Any]) -> str:
    """CRS the datacube's shared grid is built in.

    ``None`` keeps the lon/lat default; :data:`STACK_AUTO_CRS` picks the UTM
    zone containing the centre of the ground the sources cover (so the caller
    doesn't have to know which zone a site is in); anything else is a CRS name
    passed through ``rasterio`` for validation, so a typo fails here rather than
    silently producing an empty warp.
    """
    if crs is None:
        return "EPSG:4326"
    if str(crs).strip().lower() == STACK_AUTO_CRS:
        from rasterio.warp import transform_bounds  # noqa: PLC0415

        lons: list[float] = []
        lats: list[float] = []
        for ds in datasets:
            if ds.crs is None:
                raise ValueError(
                    f'crs="{STACK_AUTO_CRS}" needs each source to be georeferenced, but a '
                    "scene has no CRS. Name the output CRS outright instead."
                )
            left, bottom, right, top = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
            lons += [left, right]
            lats += [bottom, top]
        return _utm_epsg((min(lons) + max(lons)) / 2.0, (min(lats) + max(lats)) / 2.0)

    from rasterio.crs import CRS  # noqa: PLC0415
    from rasterio.errors import CRSError  # noqa: PLC0415

    try:
        return CRS.from_user_input(crs).to_string()
    # A malformed CRS surfaces either as rasterio's CRSError or, for something
    # like "EPSG:not-a-code", as the ValueError/TypeError of parsing it.
    except (CRSError, ValueError, TypeError) as exc:
        raise ValueError(
            f"crs={crs!r} is not a CRS rasterio recognizes. Pass an EPSG code "
            f'("EPSG:32633"), a PROJ/WKT string, or "{STACK_AUTO_CRS}" to pick '
            "the site's UTM zone automatically."
        ) from exc


def _stack_bounds(
    vrt_bounds: list[Any], extent: str, bbox: BBox | None, crs: str = "EPSG:4326"
) -> tuple[float, float, float, float]:
    """Window the datacube covers, in the units of the cube's own ``crs``.

    ``extent`` picks intersection (every cell has a full series) or union (no
    ground is dropped); ``bbox``, when given, clips whichever was chosen. The
    public API takes ``bbox`` in lon/lat whatever the cube's CRS, so it is
    transformed here -- and reported in the caller's own units if it misses.
    """
    if extent not in STACK_EXTENTS:
        raise ValueError(f"extent must be one of {STACK_EXTENTS}, got {extent!r}.")
    if extent == "intersection":
        left = max(b.left for b in vrt_bounds)
        bottom = max(b.bottom for b in vrt_bounds)
        right = min(b.right for b in vrt_bounds)
        top = min(b.top for b in vrt_bounds)
        if left >= right or bottom >= top:
            raise ValueError(
                "Footprints do not all overlap, so the intersection is empty. "
                "Stack acquisitions of the same site (e.g. items from one Umbra "
                'task), or pass extent="union" to keep every scene\'s own ground '
                "and fill the rest with NaN."
            )
    else:
        left = min(b.left for b in vrt_bounds)
        bottom = min(b.bottom for b in vrt_bounds)
        right = max(b.right for b in vrt_bounds)
        top = max(b.top for b in vrt_bounds)

    if bbox is not None:
        window = bbox
        if crs != "EPSG:4326":
            from rasterio.warp import transform_bounds  # noqa: PLC0415

            window = transform_bounds("EPSG:4326", crs, *bbox)
        left, bottom = max(left, window[0]), max(bottom, window[1])
        right, top = min(right, window[2]), min(top, window[3])
        if left >= right or bottom >= top:
            raise ValueError(f"bbox {bbox} does not overlap the stacked acquisitions.")
    return left, bottom, right, top


def _mask_slice(np: Any, data: Any, nodata: float | None, *, db: bool) -> Any:
    """Nodata / non-positive pixels to ``NaN``, optionally on the decibel scale.

    A stack is always masked: cross-date statistics (``mean``/``std``/``diff``)
    would otherwise be poisoned by fill values, and with ``extent="union"`` the
    ground a slice doesn't cover has to read as "no observation", not as zero.
    """
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    invalid |= data <= 0
    if db:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(invalid, np.nan, 20.0 * np.log10(data)).astype("float32")
    return np.where(invalid, np.nan, data).astype("float32")


class _StackGrid(NamedTuple):
    """The shared output grid every slice of a datacube is warped onto.

    Resolved once from the sources' footprints, then enough on its own to read
    any one scene onto it -- which is what lets a lazy cube defer a slice's read
    without keeping its dataset (or the rest of the series) open.
    """

    crs: str
    left: float
    bottom: float
    right: float
    top: float
    width: int
    height: int
    xres: float
    yres: float


def _stack_grid(
    vrt_bounds: list[Any], *, extent: str, bbox: BBox | None, crs: str, max_size: int
) -> _StackGrid:
    """Shared grid for a stack: ``max_size`` on the longer side of its extent."""
    left, bottom, right, top = _stack_bounds(vrt_bounds, extent=extent, bbox=bbox, crs=crs)

    # Output grid: max_size on the longer side, aspect from that extent, so
    # every cell is the same size in the target CRS's own units.
    width, height = right - left, top - bottom
    if width >= height:
        out_w = max(int(max_size), 1)
        out_h = max(round(out_w * height / width), 1)
    else:
        out_h = max(int(max_size), 1)
        out_w = max(round(out_h * width / height), 1)
    return _StackGrid(crs, left, bottom, right, top, out_w, out_h, width / out_w, height / out_h)


class _Speckle(NamedTuple):
    """The speckle filter every slice of a cube is read through.

    Resolved once in :func:`to_stack` -- the name checked against
    :data:`~umbra_py.convert.SPECKLE_FILTERS`, the window checked odd and at
    least 3 -- so a slab read long afterwards inside a ``dask`` task carries a
    valid filter instead of failing at compute time.
    """

    name: str
    window: int


def _filter_slab(
    slab: Any, speckle: _Speckle | None, *, db: bool, looks: float | None = None
) -> Any:
    """Speckle-filter one slab, or hand it back untouched.

    The same power-domain averaging ``umbra convert --speckle-filter`` applies in
    the radar's image space, applied instead to the slab a source was read onto.
    Reusing :func:`umbra_py.convert._filter_speckle` is the point: a cube's cells
    are then filtered by the identical arithmetic (and recorded in the identical
    vocabulary) as a converted raster's pixels, so the two cannot drift on what
    ``boxcar`` or ``lee`` means.

    ``looks`` supplies ``"lee"``'s speckle parameter from a wider scope than this
    array. It arrives only on the chunked path, where "this array" is a window of
    a pass rather than the pass: :func:`_pass_looks` reads it once for the whole
    acquisition, so every window of one pass is filtered by the same parameter
    and two neighbouring windows cannot disagree about the ground they share.
    Without it -- the unchunked path, where the slab *is* the pass -- the filter
    reads the parameter off the slab itself, as it always has.

    What the filter *achieved* on this slab -- the equivalent looks either side
    of it -- is computed and dropped. It is a per-scene diagnostic, and a lazy
    cube's slabs are read one deferred task at a time with nowhere to report it
    back to; :func:`umbra_py.convert.sicd_to_geocoded_cog` is where that number
    is kept.
    """
    if speckle is None:
        return slab
    from .convert import _filter_speckle  # noqa: PLC0415

    filtered, _achieved = _filter_speckle(
        slab, decibels=db, name=speckle.name, window=speckle.window, looks=looks
    )
    return filtered


def _read_slab(
    np: Any,
    vrt: Any,
    grid: _StackGrid,
    *,
    db: bool,
    speckle: _Speckle | None,
    looks: float | None = None,
) -> Any:
    """One scene, read onto the shared grid: the unit of work a stack is made of.

    Ground the scene doesn't cover stays ``NaN``, so a ``"union"`` cube's
    padding reads as "no observation" rather than as a value.

    ``speckle`` filters the assembled slab, which is *after* the warp rather than
    before it. That ordering is forced -- this function is the first place a
    source exists on the cube's own grid -- and it is what the filter means here:
    the window averages the cells the cube reports, so the resolution it spends
    is the resolution a measurement of this cube quotes.
    """
    from rasterio.enums import Resampling  # noqa: PLC0415

    left, top, xres, yres = grid.left, grid.top, grid.xres, grid.yres
    slab = np.full((grid.height, grid.width), np.nan, dtype="float32")
    # The part of the output grid this scene actually covers. Under
    # "intersection" that is the whole grid, so every read targets the
    # identical window and shape and the slices are exactly aligned.
    ol, ob = max(left, vrt.bounds.left), max(grid.bottom, vrt.bounds.bottom)
    orr, ot = min(grid.right, vrt.bounds.right), min(top, vrt.bounds.top)
    if ol < orr and ob < ot:
        col0 = max(round((ol - left) / xres), 0)
        col1 = min(round((orr - left) / xres), grid.width)
        row0 = max(round((top - ot) / yres), 0)
        row1 = min(round((top - ob) / yres), grid.height)
        if col1 > col0 and row1 > row0:
            # Snap back to those pixel edges so the read is grid-aligned,
            # clamped inside the VRT so the window is always readable.
            win = vrt.window(
                max(left + col0 * xres, vrt.bounds.left),
                max(top - row1 * yres, vrt.bounds.bottom),
                min(left + col1 * xres, vrt.bounds.right),
                min(top - row0 * yres, vrt.bounds.top),
            )
            # List index + 3-D out_shape, dropping the band axis here:
            # rasterio's scalar-index path squeezes in place with an
            # ndarray.shape assignment, deprecated in NumPy 2.5.
            data = vrt.read(
                [1],
                window=win,
                out_shape=(1, row1 - row0, col1 - col0),
                resampling=Resampling.average,
            )[0].astype("float32")
            slab[row0:row1, col0:col1] = _mask_slice(np, data, vrt.nodata, db=db)
    return _filter_slab(slab, speckle, db=db, looks=looks)


def _chunk_spans(total: int, size: int) -> list[tuple[int, int]]:
    """Contiguous ``(start, stop)`` spans covering ``total`` rows/columns."""
    return [(start, min(start + size, total)) for start in range(0, total, size)]


def _sub_grid(grid: _StackGrid, rows: tuple[int, int], cols: tuple[int, int]) -> _StackGrid:
    """The part of a shared grid one window covers, as a grid in its own right.

    Cell size and CRS are the parent's and the edges land on its cell
    boundaries, so reading a window through this is pixel-identical to the same
    region of the whole-grid read -- which is what lets a slab be assembled from
    windows with no seam where they meet.
    """
    row0, row1 = rows
    col0, col1 = cols
    return _StackGrid(
        grid.crs,
        grid.left + col0 * grid.xres,
        grid.top - row1 * grid.yres,
        grid.left + col1 * grid.xres,
        grid.top - row0 * grid.yres,
        col1 - col0,
        row1 - row0,
        grid.xres,
        grid.yres,
    )


def _open_slab(
    url: str,
    grid: _StackGrid,
    *,
    db: bool,
    speckle: _Speckle | None = None,
    looks: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> Any:
    """Open one source and read its slab -- the deferred task of a lazy cube.

    Self-contained on purpose: a dask task runs long after :func:`to_stack`
    returned and closed the datasets it resolved the grid from, so this re-opens
    (metadata only, over range requests) rather than capturing an open handle.

    ``crop`` is the ``(row0, row1, col0, col1)`` sub-rectangle of ``grid`` to
    return, and exists for the **halo** a speckle-filtered window is read with:
    ``grid`` is then the window grown by half a filter window, and the crop
    throws that margin away *after* filtering, so the cells that survive were
    averaged over the neighbours they have on the pass rather than over a
    truncated window (see :func:`_halo_grid`).
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    with rasterio.open(_open_path(url)) as ds:
        with WarpedVRT(ds, crs=grid.crs, resampling=Resampling.average) as vrt:
            slab = _read_slab(np, vrt, grid, db=db, speckle=speckle, looks=looks)
    if crop is None:
        return slab
    row0, row1, col0, col1 = crop
    return slab[row0:row1, col0:col1]


def _halo_grid(
    grid: _StackGrid, rows: tuple[int, int], cols: tuple[int, int], pad: int
) -> tuple[_StackGrid, tuple[int, int, int, int]]:
    """One window grown by ``pad`` cells, plus where the window sits inside it.

    A filter window centred on a cell near a chunk edge needs cells the
    neighbouring chunk holds. Read the chunk alone and those cells are missing,
    so its edge averages a truncated window and the chunked cube stops equalling
    the unchunked one it is documented to equal. Growing the read by half a
    filter window on every side and cropping *after* the filter is the fix
    ``umbra chips`` uses tile by tile, and it makes the result identical rather
    than merely better: every cell that survives the crop had its whole window
    inside the array that was filtered.

    The growth is clamped at the pass's own edges, where there is nothing to grow
    into and the whole-pass filter clips its windows in exactly the same way.
    """
    row0, row1 = rows
    col0, col1 = cols
    top, bottom = max(row0 - pad, 0), min(row1 + pad, grid.height)
    left, right = max(col0 - pad, 0), min(col1 + pad, grid.width)
    return (
        _sub_grid(grid, (top, bottom), (left, right)),
        (row0 - top, row1 - top, col0 - left, col1 - left),
    )


#: Edge of the square grid of sample windows :func:`_pass_looks` reads a pass's
#: speckle statistics from, and the edge of each window in cells. The same shape
#: (and the same reasoning) as :data:`umbra_py.chips._SPECKLE_SAMPLE_GRID`: a
#: chunked build exists because the pass does not fit in memory, so its looks are
#: read from a sample of it. A pass no wider than one window is sampled *whole*
#: (:func:`_sample_starts` collapses), which is why a cube small enough to hold
#: gives the identical number either way.
_LOOKS_SAMPLE_GRID = 3
_LOOKS_SAMPLE_SIZE = 512


def _sample_starts(total: int, size: int, count: int) -> list[int]:
    """Up to ``count`` evenly spread window origins covering ``total`` cells.

    Collapses to a single origin where the span is no wider than one window, and
    de-duplicates where the spread would repeat one -- so a small grid is sampled
    once, whole, rather than ``count`` times over.
    """
    if total <= size or count < 2:
        return [0]
    last = total - size
    return sorted({round(i * last / (count - 1)) for i in range(count)})


def _pass_looks(url: str, grid: _StackGrid, *, db: bool, window: int) -> float:
    """One pass's equivalent number of looks, read once for all of its windows.

    ``"lee"`` compares each window's variability against what speckle alone would
    explain, and *what speckle alone explains* is a property of the product's
    processing -- of the pass -- not of the few hundred cells one chunk covers.
    Read per chunk it would differ across a pass, so one part of a scene would be
    smoothed harder than the part beside it and two neighbouring windows would
    disagree about the ground they share. So it is resolved here, once, and
    handed to every window of that pass (:func:`_filter_slab`) -- the same move
    :func:`umbra_py.chips._scene_speckle` makes once per acquisition.

    It is a *sample* for the reason the chunking exists at all: the pass does not
    fit in memory. Up to :data:`_LOOKS_SAMPLE_GRID`² windows of
    :data:`_LOOKS_SAMPLE_SIZE` cells are read on a fixed grid and their blocks
    are **pooled** before the percentile is taken
    (:func:`umbra_py.convert._block_enl_ratios`), which is what makes the answer
    an estimate of the pass rather than an average of nine estimates -- and one
    window at a time, since only the blocks are kept. A pass that fits inside a
    single sample window is read whole, so the number is then exactly the one the
    unchunked path reads off the slab.

    Clamped at single-look for the reason :func:`umbra_py.convert._filter_speckle`
    clamps it: no product has fewer looks than one, so a lower read is the
    estimator meeting texture, and believing it is licence to smooth structure
    away.
    """
    np = _require("numpy")
    from .convert import (  # noqa: PLC0415
        _ENL_BLOCK,
        _ENL_BLOCK_WINDOWS,
        _ENL_PERCENTILE,
        _block_enl_ratios,
        _detected_power,
    )

    # The block size `_filter_speckle` would use for this window, so the number
    # is comparable with the one an unchunked pass reads rather than differently
    # biased.
    block = max(_ENL_BLOCK, _ENL_BLOCK_WINDOWS * window)
    ratios = []
    for row0 in _sample_starts(grid.height, _LOOKS_SAMPLE_SIZE, _LOOKS_SAMPLE_GRID):
        for col0 in _sample_starts(grid.width, _LOOKS_SAMPLE_SIZE, _LOOKS_SAMPLE_GRID):
            rows = (row0, min(row0 + _LOOKS_SAMPLE_SIZE, grid.height))
            cols = (col0, min(col0 + _LOOKS_SAMPLE_SIZE, grid.width))
            sample = _open_slab(url, _sub_grid(grid, rows, cols), db=db)
            ratios.append(_block_enl_ratios(_detected_power(sample, decibels=db), block=block))
    pooled = np.concatenate(ratios) if ratios else np.empty(0, dtype="float64")
    if pooled.size == 0:
        return 1.0
    return max(float(np.percentile(pooled, _ENL_PERCENTILE)), 1.0)


def _lazy_slab(
    dask: Any,
    dask_array: Any,
    url: str,
    grid: _StackGrid,
    *,
    db: bool,
    chunk_size: int | None,
    speckle: _Speckle | None,
) -> Any:
    """One pass as a deferred dask array: the whole slab, or a grid of windows.

    ``chunk_size`` is what decides whether a *single* scene has to fit in
    memory. Without it a pass is one task, so the smallest thing anything can
    compute is a whole ``max_size²`` slab; with it the pass is cut into
    ``chunk_size``-square windows that are read (and held) independently. The
    price is request count: each window opens the source and issues its own
    range requests, so a pass costs one read per window instead of one in total.

    ``speckle`` composes with either shape, and the two things that made a window
    the hard place to filter are answered rather than approximated. A filter
    window straddling a chunk edge: each window is read with a half-window halo
    and cropped after filtering (:func:`_halo_grid`), so a filtered chunk is the
    same cells the whole-pass filter would have produced there. And ``"lee"``'s
    speckle parameter, which is a property of the pass rather than of one window:
    it is resolved once per pass (:func:`_pass_looks`) as a single deferred task
    every window of that pass depends on, so the parameter is read once however
    many windows use it.
    """
    looks: Any = None
    if speckle is not None and chunk_size is not None:
        # "lee" needs the pass's own speckle parameter, read once as a task every
        # window depends on. "boxcar" takes none -- but supplying one still says
        # "do not measure this array's looks", which on the chunked path saves a
        # per-window ENL estimate whose result `_filter_slab` discards anyway.
        looks = (
            dask.delayed(_pass_looks)(url, grid, db=db, window=speckle.window)
            if speckle.name == "lee"
            else 1.0
        )

    def task(part: _StackGrid, crop: tuple[int, int, int, int] | None = None) -> Any:
        height = part.height if crop is None else crop[1] - crop[0]
        width = part.width if crop is None else crop[3] - crop[2]
        return dask_array.from_delayed(
            dask.delayed(_open_slab)(url, part, db=db, speckle=speckle, looks=looks, crop=crop),
            shape=(height, width),
            dtype="float32",
        )

    if chunk_size is None:
        return task(grid)
    pad = speckle.window // 2 if speckle is not None else 0
    rows = _chunk_spans(grid.height, chunk_size)
    cols = _chunk_spans(grid.width, chunk_size)
    if pad == 0:
        return dask_array.block([[task(_sub_grid(grid, r, c)) for c in cols] for r in rows])
    return dask_array.block([[task(*_halo_grid(grid, r, c, pad)) for c in cols] for r in rows])


def _filtered_provenance(provenance: dict[str, str], speckle: _Speckle) -> dict[str, str]:
    """A cube's record after it filtered its own slices, or a refusal to filter twice.

    The keys are ``umbra convert``'s own (``speckle_filter`` / ``speckle_window``)
    because they describe the raster rather than who made it: a cube whose cells
    were averaged over a 5-cell window *is* a 5-window-filtered product, and
    recording it in a second vocabulary would only hide it from the check that
    matters -- :func:`_shared_provenance` refusing to difference this cube
    against an unfiltered one later.

    Which is also why filtering an already-filtered series is refused rather than
    composed. Two averagings do not make a third the record could name: a
    3-window pass filtered again over 5 cells resolves neither 3 nor 5, so the
    honest record does not exist, and a cube that understated its own resolution
    would understate it in exactly the numbers :func:`stack_stats` reports.
    """
    already = provenance.get("speckle_filter", _STEP_NOT_RUN)
    if already != _STEP_NOT_RUN:
        window = provenance.get("speckle_window", "?")
        raise ValueError(
            f"Refusing to speckle-filter a series that is already filtered ({already}, "
            f"{window}x{window} window, recorded by 'umbra convert'): averaging twice "
            "leaves a cube whose effective resolution is neither window, so the record "
            "it would carry -- and every number measured from it -- would understate "
            "what the smoothing cost. Drop speckle_filter to keep the sources' own, or "
            "stack the unfiltered products and filter here."
        )
    return {**provenance, "speckle_filter": speckle.name, "speckle_window": str(speckle.window)}


def _resolve_speckle(name: str | None, window: int) -> _Speckle | None:
    """Check a requested speckle filter at the call, before any bytes are read.

    A lazy cube defers its reads into ``dask`` tasks that run whenever something
    asks for values, so a filter validated where it is *applied* would report a
    misspelt name or an even window from inside a compute rather than from the
    call that got it wrong -- the same reason the grid is resolved eagerly.
    """
    if name is None:
        return None
    if name not in SPECKLE_FILTERS:
        raise ValueError(
            f"Unknown speckle_filter {name!r}; choose one of {', '.join(SPECKLE_FILTERS)}."
        )
    from .convert import _check_speckle_window  # noqa: PLC0415

    return _Speckle(name, _check_speckle_window(window))


def to_stack(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int = 1024,
    db: bool = False,
    extent: str = "intersection",
    crs: str | None = None,
    lazy: bool = False,
    chunk_size: int | None = None,
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
) -> xr.DataArray:
    """Co-register several acquisitions into one ``(time, y, x)`` datacube.

    The multi-date companion to :func:`to_xarray`, and the missing primitive
    between *search* and *analysis*: hand it a search result and get back a
    labelled :class:`xarray.DataArray` whose slices are pixel-aligned, so
    ``cube.mean("time")``, ``cube.std("time")`` and ``cube.diff("time")`` are
    honest per-ground-cell statistics rather than per-scene ones.

    Alignment is real work, not a reshape: Umbra's passes over a site are
    delivered in whatever UTM zone each acquisition used and at whatever extent
    it happened to cover, so every scene is warped to one shared grid derived
    from the requested ``extent`` -- **EPSG:4326** (lon/lat) by default, or the
    projected CRS ``crs`` names when the cells have to be equal-area. Only
    decimated overviews are streamed via HTTP range requests -- no full download.

    Parameters
    ----------
    items:
        The acquisitions to stack. Order doesn't matter (the result is sorted
        oldest-first); each must carry a ``datetime``.
    asset:
        Which product to read, as in :func:`to_xarray`. Stack **one**
        polarization: mixing VV and VH puts a polarization difference on the
        time axis where you'll read it as change. ``UmbraItem.polarizations``
        and ``search(polarizations=...)`` are how you keep them apart.
    bbox:
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 clipping
        the cube to a sub-area of whatever ``extent`` selected.
    max_size:
        Longest side of the output grid in pixels. The grid is shared by every
        slice, so this caps the whole cube (bytes fetched grow ~quadratically).
    db:
        Return the decibel scale (``20*log10(amplitude)``) instead of linear
        amplitude -- the radiometrically meaningful scale for differencing,
        where a ratio of backscatter becomes a subtraction.
    extent:
        One of :data:`STACK_EXTENTS`. ``"intersection"`` (default) keeps only
        the ground *every* acquisition covers, so no cell has a gap; it raises
        if the footprints don't all overlap. ``"union"`` keeps all ground *any*
        acquisition covers and fills each slice outside its own footprint with
        ``NaN``.
    crs:
        CRS of the shared output grid. ``None`` (default) builds it in lon/lat
        (EPSG:4326), whose cells are *not* equal-area -- see the note below.
        :data:`STACK_AUTO_CRS` (``"utm"``) picks the UTM zone containing the
        stacked ground, giving square metre-sized cells without your having to
        know the zone; any other value is a CRS name (``"EPSG:32633"``, a PROJ
        or WKT string) warped to as given. ``bbox`` stays lon/lat either way.
    lazy:
        Defer each pass's read into a ``dask`` task instead of streaming the
        whole series up front -- **one chunk per acquisition**. The grid is still
        resolved eagerly (the sources' footprints decide it, and a bad ``extent``
        or ``bbox`` still raises here rather than at compute time), but no pixels
        are fetched until something asks for them, and a reduction that walks the
        cube one slice at a time -- ``cube.mean("time")``,
        :func:`stack_stats`, :func:`stack_to_geotiff` -- never holds more than a
        few slices at once. That is what lifts the ceiling this function
        otherwise has: an eager cube costs ``max_size²`` × the number of
        acquisitions in RAM, so a long series has to be stacked coarse. The
        values are identical either way. Requires the ``dask`` extra
        (``pip install "umbra-py[dask]"``).
    chunk_size:
        Cut each pass into ``chunk_size``-square windows instead of reading it
        as one slab -- the *second* half of the ceiling ``lazy`` lifts. One
        chunk per acquisition makes the unit of work a whole slice, so a single
        pass at a large ``max_size`` is still read and held whole (a 8192-pixel
        grid is 256 MB of ``float32`` per slice); windowing makes the unit
        ``chunk_size²`` instead, so how sharp a cube can be stacked stops
        depending on how much of one scene fits in memory. Requires ``lazy``.
        It costs range requests -- each window opens the source and reads its
        own bytes, so a pass costs ⌈h/c⌉ × ⌈w/c⌉ reads rather than one -- which
        is why it is opt-in and why the window wants to be a decent fraction of
        the grid (512–2048), not a tile. The values are unchanged (including
        under ``speckle_filter``, which reads each window with a halo -- see it
        below for the one number a chunked ``"lee"`` estimates rather than reads).
    speckle_filter:
        Average **speckle** down before the series is assembled: one of
        :data:`~umbra_py.convert.SPECKLE_FILTERS` (``"boxcar"``, the multilook,
        or ``"lee"``, which averages only where a window is no more variable than
        speckle alone explains), applied to each pass on the shared grid in the
        power domain. Speckle is not sensor noise -- it is the interference
        pattern coherent illumination makes on a rough surface, so a single
        look's power scatters about the surface's true backscatter with a
        standard deviation *equal to its mean*, and averaging is the only thing
        that removes it. It is therefore the dominant uncertainty in every number
        :func:`stack_stats` reports from an unfiltered cube, and the reason a
        cell-by-cell difference between two passes is mostly interference rather
        than change.

        Opt-in, because what it spends is resolution: a window that averages N
        cells reports ground N cells across. Both halves are recorded in the
        cube's ``provenance`` (``speckle_filter`` / ``speckle_window``, the same
        keys ``umbra convert`` writes), so :func:`stack_stats` states the trade,
        a GeoTIFF written from the cube carries it, and a later stack refuses to
        difference this cube against an unfiltered one.

        The filtering happens *after* co-registration -- this is the first point
        a source exists on the cube's own grid -- so the window averages the
        cells the cube reports rather than the source's own pixels. Filtering
        earlier, in the radar's image space where speckle is one independent
        sample per pixel, is ``umbra convert --speckle-filter``'s job; sources
        that already record one are refused here rather than filtered twice.

        Composes with ``chunk_size``: each window is read with a half-window halo
        and cropped after filtering, so a filtered window holds the cells the
        whole-pass filter would have put there, and ``"lee"``'s speckle parameter
        is resolved once per pass rather than per window (from a fixed sample of
        it, since a chunked build is the case where the pass does not fit in
        memory -- a pass small enough to sample whole gives the identical
        number). ``boxcar`` needs no such parameter, so a chunked ``boxcar`` cube
        is cell-for-cell the unchunked one.
    speckle_window:
        Edge of the odd, centred window ``speckle_filter`` averages over, in
        cells of the shared grid. Wider removes more speckle and more detail; it
        costs no more to compute (the filters use a summed-area table).

    Returns
    -------
    xarray.DataArray
        Dimensions ``("time", "y", "x")``: ascending ``time``, descending ``y``
        (north-up) and ascending ``x`` cell-center coordinates in the cube's CRS
        (degrees by default, projected units under ``crs``), plus an ``item_id``
        coordinate along ``time`` so every slice keeps its provenance. Nodata and
        non-positive pixels are ``NaN`` and the dtype is always ``float32``.
        ``attrs`` mirror :func:`to_xarray`'s (``crs``, ``transform``,
        ``bounds``, ``units``, ``license``, ``attribution``), plus the
        ``provenance`` the sources agree on when they carry one. Backed by NumPy,
        or -- with ``lazy=True`` -- by a ``dask`` array chunked one slice per
        acquisition (or ``chunk_size``-square windows within each slice), which
        ``.compute()`` / ``.load()`` turn into the former.

    Notes
    -----
    Sources that disagree about *what their pixel values are* are refused rather
    than stacked. A raster ``umbra convert`` produced records its calibration,
    RTC model and amplitude scale in ``UMBRA_*`` GeoTIFF tags, and stacking a
    calibrated pass against an uncalibrated one (or against a published GEC,
    which carries no tags at all) would put the difference between the two
    conversions on the time axis where you would read it as change on the
    ground. The rule is :data:`MEASUREMENT_PROVENANCE_KEYS`, the refusal names
    the disagreement and the acquisitions on each side, and what the sources
    *do* agree on is carried into ``attrs["provenance"]`` -- so a measurement
    from :func:`stack_stats`, and any GeoTIFF written from the cube, can say
    which conversion produced it. ``speckle_filter=`` adds to that record rather
    than sitting outside it: a cube that averaged its own slices says so in the
    same two keys, and is refused against an unfiltered one for the same reason.

    The default lon/lat grid stretches with latitude (cells are not equal-area),
    the same quick-look approximation ``umbra change`` / ``umbra timescan`` make.
    That is fine at scene scale and for comparing a cell to *itself* across
    dates, which is what a time series does -- but it makes a cell count a poor
    proxy for an area, and it distorts distances. Pass ``crs="utm"`` (or a
    projected CRS of your own) when the answer is "how many hectares changed":
    every cell then covers the same ground, so counting them is measuring.
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    xr = _require("xarray")
    from affine import Affine  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    ordered = _stack_items(items)
    if chunk_size is not None:
        if not lazy:
            # An eager cube reads every pass whole by construction, so a window
            # size would silently do nothing rather than bound anything.
            raise ValueError("chunk_size needs lazy=True; an eager cube is read a slab at a time.")
        if int(chunk_size) < 1:
            raise ValueError(f"chunk_size must be a positive pixel count; got {chunk_size!r}.")
    speckle = _resolve_speckle(speckle_filter, speckle_window)
    if lazy:
        # Fail on the missing extra before any bytes are streamed, not after.
        dask, dask_array = _require_dask()

    urls: list[str] = []
    datasets: list[Any] = []
    vrts: list[Any] = []
    try:
        for item in ordered:
            url = item.asset_href(asset)
            if not url:
                raise AssetNotFoundError(
                    f"Item {item.id!r} has no resolvable URL for asset {asset!r}."
                )
            urls.append(url)
            ds = rasterio.open(_open_path(url))
            datasets.append(ds)

        # Before any warping: a series whose slices were made differently is not
        # a time series of one quantity, and the sources say so themselves.
        provenance = _shared_provenance(
            [_source_provenance(ds) for ds in datasets], [i.id for i in ordered]
        )
        if speckle is not None:
            provenance = _filtered_provenance(provenance, speckle)

        # Resolved once every source is open: "utm" reads the zone off the
        # ground they cover, so it needs their footprints.
        target_crs = _resolve_stack_crs(crs, datasets)
        for ds in datasets:
            # Full-resolution warp to the shared CRS: cheap to construct, and
            # nothing is read until the *decimated* windowed reads below, which
            # let GDAL serve the matching cloud-optimized GeoTIFF overview
            # instead of every full-res tile (reading a coarse VRT whole would
            # force a full-res source read -- thousands of range requests).
            vrts.append(WarpedVRT(ds, crs=target_crs, resampling=Resampling.average))

        grid = _stack_grid(
            [v.bounds for v in vrts],
            extent=extent,
            bbox=bbox,
            crs=target_crs,
            max_size=max_size,
        )
        # The grid is resolved either way -- it is what makes the slices
        # comparable, and it needs every footprint. Only the *pixels* are
        # deferred: one dask task (and so one chunk) per acquisition -- or per
        # window of one, under chunk_size -- each re-opening its own source when
        # something finally asks for values.
        data = (
            dask_array.stack(
                [
                    _lazy_slab(
                        dask, dask_array, url, grid, db=db, chunk_size=chunk_size, speckle=speckle
                    )
                    for url in urls
                ]
            )
            if lazy
            else np.stack([_read_slab(np, vrt, grid, db=db, speckle=speckle) for vrt in vrts])
        )
    finally:
        for v in vrts:
            v.close()
        for ds in datasets:
            ds.close()

    left, top, xres, yres = grid.left, grid.top, grid.xres, grid.yres
    transform = Affine(xres, 0.0, left, 0.0, -yres, top)
    xs = left + xres * (np.arange(grid.width) + 0.5)
    ys = top - yres * (np.arange(grid.height) + 0.5)
    times = np.array(
        [i.datetime.astimezone(timezone.utc).replace(tzinfo=None) for i in ordered],  # type: ignore[union-attr]
        dtype="datetime64[ns]",
    )

    return xr.DataArray(
        data,
        dims=("time", "y", "x"),
        coords={
            "time": times,
            "y": ys,
            "x": xs,
            "item_id": ("time", [i.id for i in ordered]),
        },
        name="backscatter_db" if db else "amplitude",
        attrs={
            "crs": target_crs,
            "transform": tuple(transform)[:6],
            "bounds": (grid.left, grid.bottom, grid.right, grid.top),
            "extent": extent,
            "units": "dB" if db else "amplitude",
            "long_name": "SAR backscatter (dB)" if db else "SAR amplitude",
            "product_type": asset,
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
            **({"provenance": provenance} if provenance else {}),
        },
    )


def _cell_area_m2(crs_name: str, xres: float, yres: float) -> float | None:
    """Ground area of one cell in m², or ``None`` when cells aren't equal-area.

    A geographic grid's cells shrink toward the poles, so counting them measures
    nothing; only a projected CRS with known linear units yields an area (scaled
    to metres, so a CRS in survey feet still answers in m²).
    """
    rasterio = _require("rasterio")

    crs = rasterio.crs.CRS.from_user_input(crs_name)
    if crs.is_geographic:
        return None
    factor = crs.linear_units_factor[1]
    return abs(xres * yres) * factor * factor


class _PairAccum:
    """Signed backscatter change between two dB slices, accumulated in pieces.

    Every number a change record carries is a count or a sum over the cells the
    two passes share, so the record can be built from windows of the pair as
    easily as from the pair whole -- which is what lets :func:`stack_stats`
    measure a cube it never holds a whole slice of. Fed a single window covering
    everything (:func:`_pair_change`), the arithmetic reduces to the whole-slab
    expressions it replaced, sum for sum.
    """

    def __init__(self, np: Any, *, threshold_db: float, cell_area_m2: float | None) -> None:
        self._np = np
        self._threshold_db = threshold_db
        self._cell_area_m2 = cell_area_m2
        self.compared = 0
        self._sum_delta = 0.0
        self._sum_abs_delta = 0.0
        self._brightened = 0
        self._dimmed = 0

    def add(self, earlier_db: Any, later_db: Any) -> None:
        """Fold one window of the pair in. Cells either pass missed are ignored."""
        np = self._np
        both = np.isfinite(earlier_db) & np.isfinite(later_db)
        compared = int(both.sum())
        if compared == 0:
            return
        delta = later_db[both] - earlier_db[both]
        self.compared += compared
        self._sum_delta += float(np.sum(delta))
        self._sum_abs_delta += float(np.sum(np.abs(delta)))
        self._brightened += int(np.count_nonzero(delta >= self._threshold_db))
        self._dimmed += int(np.count_nonzero(delta <= -self._threshold_db))

    def result(self) -> dict[str, Any] | None:
        """The change record, or ``None`` when the two passes share no cell."""
        compared = self.compared
        if compared == 0:
            return None
        changed = self._brightened + self._dimmed
        area = self._cell_area_m2
        return {
            "compared_cells": compared,
            "mean_delta_db": round(self._sum_delta / compared, 3),
            "mean_abs_delta_db": round(self._sum_abs_delta / compared, 3),
            "brightened_fraction": round(self._brightened / compared, 4),
            "dimmed_fraction": round(self._dimmed / compared, 4),
            "changed_fraction": round(changed / compared, 4),
            "changed_area_km2": (round(changed * area / 1e6, 4) if area is not None else None),
        }


def _pair_change(
    np: Any,
    earlier_db: Any,
    later_db: Any,
    *,
    threshold_db: float,
    cell_area_m2: float | None,
) -> dict[str, Any] | None:
    """Signed backscatter change between two co-registered dB slices.

    Only cells observed on *both* passes are compared, so ``extent="union"``
    padding never reads as change. ``None`` when the two passes share no cell.
    """
    accum = _PairAccum(np, threshold_db=threshold_db, cell_area_m2=cell_area_m2)
    accum.add(earlier_db, later_db)
    return accum.result()


def _block_lonlat(crs_name: str, centers: list[tuple[float, float]]) -> list[list[float] | None]:
    """Lon/lat of each block centre, so a block is locatable on the ground.

    The block bounds are in the cube's own CRS (metres under ``crs="utm"``),
    which is exact but unreadable; a lon/lat centre is what a consumer needs to
    put the block on a map or reverse-geocode it.
    """
    rasterio = _require("rasterio")

    if not centers:
        return []
    crs = rasterio.crs.CRS.from_user_input(crs_name)
    xs = [c[0] for c in centers]
    ys = [c[1] for c in centers]
    if crs.is_geographic:
        lons, lats = xs, ys
    else:
        from rasterio.warp import transform as warp_transform  # noqa: PLC0415

        lons, lats = warp_transform(crs, "EPSG:4326", xs, ys)
    return [
        [round(float(lon), 6), round(float(lat), 6)] for lon, lat in zip(lons, lats, strict=True)
    ]


def _grid_text(rows: int, cols: int, deltas: dict[tuple[int, int], float | None]) -> str:
    """North-up ASCII heat-grid of signed dB change, one cell per block.

    The same shape ``umbra change --narrate`` grounds its narration on
    (:meth:`umbra_py.narrate.ChangeStats.to_grid_text`), so a model reading both
    reductions sees one spatial vocabulary. ``.`` marks a block never observed on
    both of the compared passes.
    """
    lines = []
    for r in range(rows):
        cells = []
        for c in range(cols):
            value = deltas.get((r, c))
            cells.append("   . " if value is None else f"{value:+5.1f}")
        lines.append(" ".join(cells))
    return "\n".join(lines)


class _BlockChanges:
    """Per-block change accumulated one *piece* at a time, not one block at a time.

    The same reduction :func:`_spatial_breakdown` reports, turned inside out: it
    is fed consecutive pairs of passes as they arrive — and, when the cube is
    measured window by window, one window of a pair at a time — so the cube it
    measures never has to exist all at once. Each block's record is a
    :class:`_PairAccum`, whose numbers are counts and sums over the cells the two
    passes share, so the loop order (and how much of a pass is resident) cannot
    move them.

    Block geometry comes from ``narrate``'s helpers, so a block's ``compass``
    label means the same thing in both of the library's change reductions.
    """

    def __init__(
        self,
        np: Any,
        *,
        shape: tuple[int, int],
        blocks: int,
        ids: list[str],
        stamps: list[str],
        threshold_db: float,
        cell_area_m2: float | None,
    ) -> None:
        from .narrate import _split_slices  # noqa: PLC0415

        if blocks < 1:
            raise ValueError(f"blocks must be >= 1, got {blocks}.")
        self._np = np
        self._ids = ids
        self._stamps = stamps
        self._threshold_db = threshold_db
        self._cell_area_m2 = cell_area_m2
        self._whole = ((0, shape[0]), (0, shape[1]))
        self.blocks = blocks
        self.row_slices = _split_slices(shape[0], blocks)
        self.col_slices = _split_slices(shape[1], blocks)
        keys = [(r, c) for r in range(len(self.row_slices)) for c in range(len(self.col_slices))]
        self._steps: dict[tuple[int, int], list[_PairAccum]] = {
            key: [self._accum() for _ in range(max(len(ids) - 1, 0))] for key in keys
        }
        self._net: dict[tuple[int, int], _PairAccum] = {key: self._accum() for key in keys}

    def _accum(self) -> _PairAccum:
        return _PairAccum(
            self._np, threshold_db=self._threshold_db, cell_area_m2=self._cell_area_m2
        )

    def _parts(self, window: tuple[tuple[int, int], tuple[int, int]] | None) -> Any:
        """Each block this window touches, as a slice pair in *window* coordinates.

        A window is a rectangle of the shared grid and a block is another, so the
        part of a block a window carries is their overlap — expressed relative to
        the window, because that is the array the caller holds. A whole-grid
        window yields every block in full, i.e. the block slices themselves.
        """
        (row0, row1), (col0, col1) = window if window is not None else self._whole
        for r, rsl in enumerate(self.row_slices):
            top, bottom = max(rsl.start, row0), min(rsl.stop, row1)
            if top >= bottom:
                continue
            rows = slice(top - row0, bottom - row0)
            for c, csl in enumerate(self.col_slices):
                left, right = max(csl.start, col0), min(csl.stop, col1)
                if left >= right:
                    continue
                yield (r, c), rows, slice(left - col0, right - col0)

    def add_step(
        self,
        index: int,
        earlier_db: Any,
        later_db: Any,
        window: tuple[tuple[int, int], tuple[int, int]] | None = None,
    ) -> None:
        """Record every block's move from pass ``index - 1`` to pass ``index``."""
        for key, rows, cols in self._parts(window):
            self._steps[key][index - 1].add(earlier_db[rows, cols], later_db[rows, cols])

    def add_net(
        self,
        first_db: Any,
        last_db: Any,
        window: tuple[tuple[int, int], tuple[int, int]] | None = None,
    ) -> None:
        """Record every block's net first → last change."""
        for key, rows, cols in self._parts(window):
            self._net[key].add(first_db[rows, cols], last_db[rows, cols])

    def steps(self) -> dict[tuple[int, int], list[dict[str, Any]]]:
        """Each block's consecutive pass-to-pass sequence, oldest first.

        A step a block was never observed on both sides of is dropped rather than
        reported as zero change.
        """
        out: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for key, accums in self._steps.items():
            records = []
            for index, accum in enumerate(accums, start=1):
                step = accum.result()
                if step is None:
                    continue
                records.append(
                    {
                        "from_item_id": self._ids[index - 1],
                        "from_datetime": self._stamps[index - 1],
                        "to_item_id": self._ids[index],
                        "to_datetime": self._stamps[index],
                        "mean_delta_db": step["mean_delta_db"],
                        "changed_fraction": step["changed_fraction"],
                        "changed_area_km2": step["changed_area_km2"],
                    }
                )
            out[key] = records
        return out

    def net(self) -> dict[tuple[int, int], dict[str, Any] | None]:
        """Each block's net first → last change (``None`` where never compared)."""
        return {key: accum.result() for key, accum in self._net.items()}


def _spatial_breakdown(
    changes: _BlockChanges,
    *,
    transform: tuple[float, ...],
    crs_name: str,
    block_series: bool = False,
) -> dict[str, Any]:
    """Per-block change over the whole series — *where* it happened, and *when*.

    The merge of the library's two change reductions:
    :func:`~umbra_py.narrate.compute_change_stats` cuts *two* passes into a
    coarse grid to say where change sits, and :func:`stack_stats` walks the whole
    series to say when it happened. This cuts *every* pass into that same grid,
    so each block reports its net first-to-last change **and** the consecutive
    interval it moved most in.

    With ``block_series`` each block also keeps the *whole* consecutive sequence
    it was reduced from, not only its peak — the same records, none discarded.
    """
    from .narrate import _compass_label  # noqa: PLC0415

    blocks = changes.blocks
    xres, _, xoff, _, yres, yoff = transform
    net_by_block = changes.net()
    steps_by_block = changes.steps()

    records: list[dict[str, Any]] = []
    centers: list[tuple[float, float]] = []
    deltas: dict[tuple[int, int], float | None] = {}
    for r, rsl in enumerate(changes.row_slices):
        for c, csl in enumerate(changes.col_slices):
            net = net_by_block[(r, c)]
            # The block's consecutive pass-to-pass sequence. The peak interval —
            # the block's "when" answer — is the largest-magnitude step in it.
            steps = steps_by_block[(r, c)]
            peak_interval = max(steps, key=lambda s: abs(s["mean_delta_db"]), default=None)

            xs = (xoff + csl.start * xres, xoff + csl.stop * xres)
            ys = (yoff + rsl.start * yres, yoff + rsl.stop * yres)
            centers.append((sum(xs) / 2.0, sum(ys) / 2.0))
            deltas[(r, c)] = net["mean_delta_db"] if net else None
            records.append(
                {
                    "row": r,
                    "col": c,
                    "compass": _compass_label(r, c, blocks, blocks),
                    "bounds": [
                        float(min(xs)),
                        float(min(ys)),
                        float(max(xs)),
                        float(max(ys)),
                    ],
                    # Filled in below, in one transform for the whole grid.
                    "center_lonlat": None,
                    "cells": int((rsl.stop - rsl.start) * (csl.stop - csl.start)),
                    "net_change": net,
                    "peak_interval": peak_interval,
                    # Only when asked: N x N blocks x (passes - 1) steps is the
                    # largest thing this reduction can emit.
                    **({"series": steps} if block_series else {}),
                }
            )

    for record, center in zip(records, _block_lonlat(crs_name, centers), strict=True):
        record["center_lonlat"] = center

    moved = [r for r in records if r["net_change"] is not None]
    peak = max(moved, key=lambda r: abs(r["net_change"]["mean_delta_db"]), default=None)
    return {
        "grid_rows": blocks,
        "grid_cols": blocks,
        "bounds_crs": crs_name,
        "peak_block": (
            {
                "row": peak["row"],
                "col": peak["col"],
                "compass": peak["compass"],
                "center_lonlat": peak["center_lonlat"],
                "mean_delta_db": peak["net_change"]["mean_delta_db"],
                "direction": ("brighter" if peak["net_change"]["mean_delta_db"] >= 0 else "dimmer"),
                "peak_interval": peak["peak_interval"],
            }
            if peak is not None
            else None
        ),
        "grid_text": _grid_text(blocks, blocks, deltas),
        "blocks": records,
    }


def _pass_slabs(
    np: Any,
    cube: xr.DataArray,
    index: int,
    *,
    units: str,
    window: tuple[tuple[int, int], tuple[int, int]] | None = None,
) -> tuple[Any, Any]:
    """One pass of a cube as ``(values, dB view)``, read a single slice at a time.

    The slice is what is materialised — for a lazy cube that computes exactly
    one chunk, and for an eager one it copies a single slab instead of the whole
    series. ``window`` narrows that further to one ``(rows, cols)`` rectangle of
    the pass, so a chunked cube materialises a single window instead of a whole
    slice. Change is a ratio of backscatter, i.e. a difference on the log scale,
    so a linear cube gets the dB view it should be compared on; a dB cube is
    already there and the two are the same array.
    """
    part = cube.isel(time=index)
    if window is not None:
        (row0, row1), (col0, col1) = window
        part = part.isel(y=slice(row0, row1), x=slice(col0, col1))
    slab = np.asarray(part.values, dtype="float64")
    if units == "dB":
        return slab, slab
    with np.errstate(divide="ignore", invalid="ignore"):
        return slab, np.where(slab > 0, 20.0 * np.log10(slab), np.nan)


class _QuantileSketch:
    """A mergeable fixed-width histogram of one pass, on the decibel axis.

    The one part of the reduction that is *not* a count or a sum: a median or a
    percentile needs the whole distribution, which is exactly what a cube
    measured window by window never has in one place. This keeps the
    distribution's *shape* instead of its values — one counter per
    ``_QUANTILE_BIN_DB``-wide bin, merged as windows arrive — so the estimate
    costs occupied bins rather than cells, and its error is bounded by the bin
    width rather than by how the cube happened to be chunked.

    Decibels are the axis whatever the cube holds, because backscatter spans
    orders of magnitude: a fixed-width bin is a fixed *ratio* of amplitude, so
    the estimate is equally good at the dark and bright ends. Quantiles survive
    the monotone transform, so a linear cube's percentile is its dB percentile
    read back as amplitude.
    """

    def __init__(self, np: Any) -> None:
        self._np = np
        self._bins: dict[int, int] = {}

    def add(self, db_values: Any) -> None:
        """Fold one window's finite dB values into the histogram."""
        np = self._np
        observed = db_values[np.isfinite(db_values)]
        if observed.size == 0:
            return
        indices, counts = np.unique(
            np.floor(observed / _QUANTILE_BIN_DB).astype("int64"), return_counts=True
        )
        bins = self._bins
        for index, count in zip(indices.tolist(), counts.tolist(), strict=True):
            bins[index] = bins.get(index, 0) + count

    def quantile(self, q: float) -> float:
        """The ``q``-quantile in decibels, of whatever has been added so far.

        Within the bin that holds the answer the counted values are assumed
        evenly spread, which is what keeps the estimate continuous as a
        distribution shifts rather than stepping bin to bin.
        """
        total = sum(self._bins.values())
        target = q * (total - 1)
        seen = 0
        for index in sorted(self._bins):
            count = self._bins[index]
            if seen + count > target:
                offset = min(max((target - seen + 0.5) / count, 0.0), 1.0)
                return (index + offset) * _QUANTILE_BIN_DB
            seen += count
        return (max(self._bins) + 1) * _QUANTILE_BIN_DB


class _DistAccum:
    """One pass's distribution, accumulated window by window.

    Count and mean are sums; the spread is merged with Chan's parallel variance
    update rather than recomputed, so neither depends on how the pass was cut up
    (a one-window pass reproduces the whole-slab ``mean``/``std`` exactly). Only
    the percentiles are estimated — see :class:`_QuantileSketch`.
    """

    def __init__(self, np: Any, *, units: str) -> None:
        self._np = np
        self._units = units
        self._sketch = _QuantileSketch(np)
        self.count = 0
        self._mean = 0.0
        self._m2 = 0.0

    def add(self, values: Any, db_values: Any) -> None:
        np = self._np
        observed = values[np.isfinite(values)]
        count = int(observed.size)
        if count:
            mean = float(np.mean(observed))
            m2 = float(np.sum((observed - mean) ** 2))
            total = self.count + count
            delta = mean - self._mean
            self._mean += delta * count / total
            self._m2 += m2 + delta * delta * self.count * count / total
            self.count = total
        self._sketch.add(db_values)

    def _percentile(self, q: float) -> float:
        """A dB quantile back in the cube's own units (``10**(dB/20)`` if linear)."""
        db = self._sketch.quantile(q)
        return round(db if self._units == "dB" else 10.0 ** (db / 20.0), 3)

    def result(self) -> dict[str, Any]:
        """The pass's distribution, or all-``None`` when it observed nothing."""
        if not self.count:
            return dict.fromkeys(("mean", "median", "std", "p5", "p95"))
        return {
            "mean": round(self._mean, 3),
            "median": self._percentile(0.5),
            "std": round((self._m2 / self.count) ** 0.5, 3),
            "p5": self._percentile(0.05),
            "p95": self._percentile(0.95),
        }


def _measure_whole(
    np: Any,
    cube: xr.DataArray,
    *,
    units: str,
    ids: list[str],
    stamps: list[str],
    cells: int,
    threshold_db: float,
    cell_area_m2: float | None,
    changes: _BlockChanges | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Walk the series one whole slice at a time, exactly as read.

    Holds at most the first, the previous and the current pass, so the
    reduction's memory is set by the *grid* rather than by the length of the
    series, and a lazy (dask-backed) cube is measured by computing one slice per
    step instead of materialising the whole thing.
    """
    passes: list[dict[str, Any]] = []
    first_db: Any = None
    previous_db: Any = None
    for i, (item_id, stamp) in enumerate(zip(ids, stamps, strict=True)):
        slab, db_slab = _pass_slabs(np, cube, i, units=units)
        finite = np.isfinite(slab)
        n_valid = int(finite.sum())
        observed = slab[finite]
        record: dict[str, Any] = {
            "item_id": item_id,
            "datetime": stamp,
            "valid_cells": n_valid,
            "valid_fraction": round(n_valid / cells, 4) if cells else 0.0,
            "mean": round(float(np.mean(observed)), 3) if n_valid else None,
            "median": round(float(np.median(observed)), 3) if n_valid else None,
            "std": round(float(np.std(observed)), 3) if n_valid else None,
            "p5": round(float(np.percentile(observed, 5)), 3) if n_valid else None,
            "p95": round(float(np.percentile(observed, 95)), 3) if n_valid else None,
            "change_vs_previous": (
                _pair_change(
                    np,
                    previous_db,
                    db_slab,
                    threshold_db=threshold_db,
                    cell_area_m2=cell_area_m2,
                )
                if i
                else None
            ),
        }
        passes.append(record)
        if i:
            if changes is not None:
                changes.add_step(i, previous_db, db_slab)
        else:
            first_db = db_slab
        previous_db = db_slab

    if len(passes) < 2:
        return passes, None
    if changes is not None:
        changes.add_net(first_db, previous_db)
    return passes, _pair_change(
        np, first_db, previous_db, threshold_db=threshold_db, cell_area_m2=cell_area_m2
    )


def _measure_windowed(
    np: Any,
    cube: xr.DataArray,
    *,
    units: str,
    ids: list[str],
    stamps: list[str],
    cells: int,
    threshold_db: float,
    cell_area_m2: float | None,
    changes: _BlockChanges | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Walk the *windows* of the series, so no whole slice is ever resident.

    The loop is turned inside out relative to :func:`_measure_whole`: the outer
    step is one window of the shared grid — the cube's own chunks
    (:func:`to_stack`'s ``chunk_size``) — and the series is walked inside it, so
    what is held is three windows rather than three slices. Every statistic
    except the percentiles is a count or a sum and so folds in piece by piece;
    the percentiles come from a histogram of the same pieces
    (:class:`_QuantileSketch`).
    """
    count = len(ids)

    def accum() -> _PairAccum:
        return _PairAccum(np, threshold_db=threshold_db, cell_area_m2=cell_area_m2)

    dists = [_DistAccum(np, units=units) for _ in range(count)]
    # Indexed like the passes: ``steps[i]`` is pass ``i`` against pass ``i - 1``,
    # so ``steps[0]`` stays empty and reports nothing, like the first pass's
    # ``change_vs_previous``.
    steps = [accum() for _ in range(count)]
    net = accum()

    for window in _cube_windows(cube):
        first_db: Any = None
        previous_db: Any = None
        for i in range(count):
            slab, db_slab = _pass_slabs(np, cube, i, units=units, window=window)
            dists[i].add(slab, db_slab)
            if i:
                steps[i].add(previous_db, db_slab)
                if changes is not None:
                    changes.add_step(i, previous_db, db_slab, window)
            else:
                first_db = db_slab
            previous_db = db_slab
        if count > 1:
            net.add(first_db, previous_db)
            if changes is not None:
                changes.add_net(first_db, previous_db, window)

    passes = [
        {
            "item_id": item_id,
            "datetime": stamp,
            "valid_cells": dists[i].count,
            "valid_fraction": round(dists[i].count / cells, 4) if cells else 0.0,
            **dists[i].result(),
            "change_vs_previous": steps[i].result() if i else None,
        }
        for i, (item_id, stamp) in enumerate(zip(ids, stamps, strict=True))
    ]
    return passes, net.result() if count > 1 else None


def stack_stats(
    cube: xr.DataArray,
    *,
    change_threshold_db: float = 3.0,
    blocks: int = 0,
    block_series: bool = False,
    windowed: bool = False,
) -> dict[str, Any]:
    """Summarize a datacube's *time* axis as a JSON-ready statistics series.

    The reporting companion to :func:`to_stack`: the cube itself is an array, but
    the question a multi-date search is usually asking — *how did this site
    change, and by how much?* — has a small numeric answer. This reduces the
    ``(time, y, x)`` cube to one record per pass (distribution statistics plus
    the signed change against the pass before it) and one net baseline → latest
    record, all plain JSON so it fits a CLI print, a manifest, or an agent tool
    result without carrying pixels around.

    Complementary to :func:`~umbra_py.narrate.compute_change_stats`, which cuts
    *two* passes into spatial blocks to say **where** change sits. This walks the
    whole series to say **when** it happened and **how much** ground moved — and
    with ``blocks=N`` it does both at once, cutting every pass into the same
    coarse grid so each block answers where *and* when.

    Parameters
    ----------
    cube:
        A cube from :func:`to_stack` — dimensions ``("time", "y", "x")`` with the
        ``item_id`` coordinate and ``crs`` / ``transform`` / ``units`` attributes
        it sets. Slices must already be co-registered; nothing is re-gridded here.
    change_threshold_db:
        How many decibels a cell has to move between two passes to count as
        changed. 3 dB (a doubling of backscatter power) is the same default
        ``umbra change --narrate`` grounds its narration on.
    blocks:
        Cut the cube into a ``blocks`` × ``blocks`` grid and report each block
        separately (0, the default, skips the breakdown entirely). A scene-wide
        mean hides a change that moved one corner hard, so this is what turns
        "the site changed 1.4 dB" into "the northeast corner brightened 9 dB,
        between the March and April passes".
    block_series:
        Keep each block's **whole** pass-to-pass sequence, not just the interval
        it moved most in. The steps are computed either way — this only decides
        whether they are reported — so it costs payload, not arithmetic:
        ``blocks`` × ``blocks`` × (``count`` − 1) records at most. Requires
        ``blocks``. Ask for it when the question is the *shape* of a block's
        history — did it move once and stay, or drift every pass? — which a
        single peak interval cannot answer.
    windowed:
        Measure the cube one **window** at a time instead of one pass at a time,
        following the cube's own chunks (:func:`to_stack`'s ``chunk_size``). The
        default reads a whole slice per pass, so a cube stacked sharper than
        memory can be *written* but not measured; this drops the resident
        footprint to three windows and lifts that last ceiling.

        The trade is stated rather than hidden: every count, mean, standard
        deviation and change number is still exact (they are sums, so a window
        folds in), but the per-pass ``median``/``p5``/``p95`` become histogram
        estimates, good to about one ``_QUANTILE_BIN_DB`` bin — a quantile needs
        the whole distribution, which is the one thing a window-by-window walk
        never has.
        The summary says so (``quantile_method`` / ``quantile_bin_db`` plus a
        caveat), so a consumer can tell the two kinds of number apart. An
        unchunked cube is one window, i.e. the default read with estimated
        percentiles.

    Returns
    -------
    dict
        ``{count, units, product_type, grid, passes, net_change,
        change_threshold_db, license, attribution, caveats}``, plus
        ``provenance`` when the cube carries one (:func:`to_stack`) — the
        conversion its slices were made by, which is also what decides whether
        the first caveat calls these decibels relative or calibrated. Each entry in
        ``passes`` carries ``item_id``, ``datetime``, ``valid_fraction`` and the
        distribution of that pass (``mean``/``median``/``std``/``p5``/``p95``, in
        the cube's own ``units``), plus ``change_vs_previous`` (``None`` for the
        first pass). ``net_change`` compares the first pass to the last.

        With ``blocks``, an extra ``spatial`` key carries the grid: one record
        per block with its ``row``/``col``, a plain-language ``compass`` label,
        ``bounds`` in the cube's CRS, a ``center_lonlat`` to map or geocode it
        by, its ``net_change`` (first → last, same fields as the top-level one)
        and its ``peak_interval`` — the consecutive pair of passes that block
        moved most between, named by item id and timestamp. Alongside them,
        ``peak_block`` names the block that moved most overall and ``grid_text``
        renders the net signed change as a north-up ASCII heat-grid. With
        ``block_series`` each block additionally carries the ``series`` those
        peaks were picked from — every consecutive step, oldest first, in the
        same shape as ``peak_interval``.

        Change is **always** reported in decibels — a ratio of backscatter is a
        difference on the log scale — whether the cube holds dB or linear
        amplitude, so the numbers mean the same thing either way.
        ``changed_area_km2`` is ``None`` unless the cube's grid is projected
        (see :func:`to_stack`'s ``crs``), because counting geographic cells
        measures nothing.
    """
    np = _require("numpy")

    if cube.ndim != 3:
        raise ValueError(f"stack_stats needs a (time, y, x) cube; got {cube.ndim}D.")
    if block_series and not blocks:
        # The series lives on a block, so asking for one without the other is a
        # request that can't be answered — say so before doing any of the work,
        # rather than silently dropping the flag.
        raise ValueError("block_series needs a blocks grid; pass blocks=N as well.")

    units = str(cube.attrs.get("units", "amplitude"))

    xres, _, _, _, yres, _ = cube.attrs["transform"]
    crs_name = str(cube.attrs["crs"])
    area = _cell_area_m2(crs_name, xres, yres)
    height, width = int(cube.shape[1]), int(cube.shape[2])
    cells = height * width

    stamps = [f"{str(t)[:19]}Z" for t in cube["time"].values]
    ids = [str(v) for v in cube["item_id"].values]

    changes = (
        _BlockChanges(
            np,
            shape=(height, width),
            blocks=blocks,
            ids=ids,
            stamps=stamps,
            threshold_db=change_threshold_db,
            cell_area_m2=area,
        )
        if blocks
        else None
    )

    passes, net_change = (_measure_windowed if windowed else _measure_whole)(
        np,
        cube,
        units=units,
        ids=ids,
        stamps=stamps,
        cells=cells,
        threshold_db=change_threshold_db,
        cell_area_m2=area,
        changes=changes,
    )

    # What the cube's own sources say they are (empty for Umbra's published
    # products, which carry no conversion tags). The first two caveats are
    # statements about the pixel values, so they are answerable from it rather
    # than fixed: a calibrated, terrain-flattened cube has earned a narrower one.
    provenance = dict(cube.attrs.get("provenance") or {})
    calibration = provenance.get("calibration", "none")
    rtc_model = provenance.get("rtc_model", "none")

    caveats = [
        "Umbra's open products are not radiometrically calibrated, so decibel "
        "values are relative: compare a cell to itself across dates, not to "
        "another site or sensor."
        if calibration == "none"
        else f"These values carry the source products' own {calibration} radiometric "
        "calibration (recorded by 'umbra convert' and read back off the rasters), so "
        "a decibel value is a physical backscatter coefficient rather than relative "
        "brightness.",
        "A decibel change is a measurement, not an interpretation -- differing "
        "look geometry between passes moves backscatter too."
        if rtc_model == "none"
        else "A decibel change is a measurement, not an interpretation -- the terrain "
        f"component of the look geometry was flattened ({rtc_model} model), but "
        "incidence-angle-dependent scattering still moves backscatter between passes.",
    ]
    noise_subtraction = provenance.get("noise_subtraction", "none")
    if noise_subtraction != "none":
        # Only said when it was earned. The default -- a floor left in -- is the
        # assumption the first caveat's "relative" already covers, and saying so
        # for every published product would be noise about noise.
        caveats.append(
            "The receiver's own thermal-noise floor was subtracted from these values "
            "(recorded by 'umbra convert'), so a dark cell reports the ground rather "
            "than the sensor's sensitivity limit -- except where the subtraction drove "
            "it to the floor, which is that limit."
        )
    if noise_subtraction == "estimated":
        # The number that came off was inferred from each scene, not read from
        # it, and a caveat that did not say so would let an estimate be quoted
        # with a measurement's confidence.
        caveats.append(
            "That floor was estimated from each scene's own darkest pixels rather "
            "than read from the products' noise metadata, so it is one constant per "
            "pass (it cannot follow the across-swath variation) and it assumes every "
            "scene contained dark ground to measure -- over uniformly bright imagery "
            "it takes real backscatter off."
        )
    if noise_subtraction == "estimated-range":
        # Same inference, fitted across range rather than taken once: the first
        # limit of the constant estimate is gone, the second is not, and a caveat
        # that reused the constant model's wording would understate one and
        # overstate the other.
        caveats.append(
            "That floor was estimated from each scene's own darkest pixels rather "
            "than read from the products' noise metadata, fitted across range so it "
            "follows the swath -- but it is still an inference, and it assumes every "
            "pass contained dark ground somewhere along range to read: over uniformly "
            "bright imagery it takes real backscatter off."
        )
    speckle_filter = provenance.get("speckle_filter", "none")
    if speckle_filter != "none":
        # Said because it cuts both ways: a filtered cube's per-cell numbers are
        # better estimates *and* coarser measurements, and which of the two a
        # reader cares about depends on the block size they are quoting.
        window = provenance.get("speckle_window", "?")
        # Unattributed on purpose: the filter is either the sources' own (`umbra
        # convert --speckle-filter`, in the radar's image space) or this cube's
        # (`to_stack(speckle_filter=...)`, on the shared grid), and the trade the
        # caveat states is the same trade either way.
        caveats.append(
            f"Every pass was speckle-filtered ({speckle_filter}, {window}x{window} "
            "window), so a cell's decibels are a less noisy estimate of its surface "
            "than a single look is -- and its effective resolution is that of a "
            f"{window}-wide window, so detail smaller than that has been averaged "
            "away rather than measured."
        )
    if area is None:
        caveats.append(
            f"The cube's grid is geographic ({crs_name}), whose cells are not "
            "equal-area, so no changed area is reported. Rebuild the cube with "
            f"crs={STACK_AUTO_CRS!r} (or a projected CRS) to measure area."
        )
    if windowed:
        caveats.append(
            "The cube was measured window by window, so each pass's median, p5 "
            f"and p95 are histogram estimates, good to about the {_QUANTILE_BIN_DB} "
            "dB bin they were counted in. Every count, mean, standard deviation "
            "and change number is exact."
        )

    summary: dict[str, Any] = {
        "count": len(passes),
        "units": units,
        "product_type": cube.attrs.get("product_type"),
        "change_threshold_db": change_threshold_db,
        # Present only when the sources recorded one, so its absence means "these
        # are the published products as delivered" rather than "unknown".
        **({"provenance": provenance} if provenance else {}),
        # Only when the percentiles are estimates: a summary that doesn't say
        # this is one whose numbers are all exact.
        **(
            {"quantile_method": "histogram", "quantile_bin_db": _QUANTILE_BIN_DB}
            if windowed
            else {}
        ),
        "grid": {
            "crs": crs_name,
            "width": width,
            "height": height,
            "cell_size": [abs(float(xres)), abs(float(yres))],
            "cell_area_m2": round(area, 4) if area is not None else None,
            "bounds": [float(v) for v in cube.attrs["bounds"]],
            "extent": cube.attrs.get("extent"),
        },
        "passes": passes,
        "net_change": net_change,
        "license": DATA_LICENSE,
        "attribution": ATTRIBUTION,
        "caveats": caveats,
    }
    if changes is not None:
        summary["spatial"] = _spatial_breakdown(
            changes,
            transform=tuple(cube.attrs["transform"]),
            crs_name=crs_name,
            block_series=block_series,
        )
    return summary


def stack_to_geotiff(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int = 1024,
    db: bool = False,
    extent: str = "intersection",
    crs: str | None = None,
    lazy: bool = False,
    chunk_size: int | None = None,
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
) -> Path:
    """Co-register several acquisitions and write the cube to a GeoTIFF.

    The file-producing companion to :func:`to_stack`, mirroring what
    :func:`to_geotiff` is to :func:`to_xarray`. The output is a multi-band
    ``float32`` GeoTIFF in the cube's CRS (EPSG:4326 unless ``crs`` names
    another, e.g. ``"utm"`` for equal-area cells) -- **one band per acquisition,
    oldest first** -- with each band described by its acquisition timestamp and
    the item ids carried in the file tags, so the time axis survives the trip
    into QGIS, GDAL or any GIS. Nodata is ``NaN``; deflate-compressed and tiled.

    ``lazy`` (see :func:`to_stack`) makes this the memory-bounded path to a big
    file: bands are written one at a time, so a series long enough to blow up an
    in-memory cube still writes, at the resolution it deserves. Add
    ``chunk_size`` and a band is written one *window* at a time too, so a grid
    too large for one slice to be resident still writes. The file is
    byte-identical however it was read.

    ``speckle_filter`` (see :func:`to_stack`) averages speckle down in every
    band before it is written, and the file records that it did -- so the raster
    a GIS opens says what its values are, and re-stacking it against an
    unfiltered product is refused rather than measured.
    """
    cube = to_stack(
        items,
        asset=asset,
        bbox=bbox,
        max_size=max_size,
        db=db,
        extent=extent,
        crs=crs,
        lazy=lazy,
        chunk_size=chunk_size,
        speckle_filter=speckle_filter,
        speckle_window=speckle_window,
    )
    return _write_stack_geotiff(cube, dest)


def _cube_windows(cube: xr.DataArray) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """The ``(rows, cols)`` spans a slice is consumed in: the cube's own chunks.

    A cube chunked *within* a slice (:func:`to_stack`'s ``chunk_size``) is
    written -- and, under ``stack_stats(windowed=True)``, measured -- window by
    window, so the consumer materialises a chunk rather than a whole band.
    Anything else -- an eager cube, or a lazy one chunked only across the series
    -- reports the single whole-band window, which is the read this function
    replaced.
    """
    height, width = int(cube.shape[1]), int(cube.shape[2])
    chunks = getattr(getattr(cube, "data", None), "chunks", None)
    if not chunks or len(chunks) != 3:
        return [((0, height), (0, width))]

    def spans(sizes: tuple[int, ...]) -> list[tuple[int, int]]:
        out, start = [], 0
        for size in sizes:
            out.append((start, start + int(size)))
            start += int(size)
        return out

    return [(rows, cols) for rows in spans(chunks[1]) for cols in spans(chunks[2])]


def _write_stack_geotiff(cube: xr.DataArray, dest: str | os.PathLike) -> Path:
    """Write a :func:`to_stack` cube out as the multi-band GeoTIFF.

    Split from :func:`stack_to_geotiff` so a caller that already holds the cube
    (``umbra stack --stats``, which also measures it) writes the file without
    stacking the series a second time.

    Bands are read and written one at a time -- and one *window* at a time when
    the cube carries windows (:func:`to_stack`'s ``chunk_size``) -- so a lazy
    cube streams to disk a chunk at a time rather than being materialised whole
    to be copied out.
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    from affine import Affine  # noqa: PLC0415
    from rasterio.windows import Window  # noqa: PLC0415

    stamps = [str(t)[:19] for t in cube["time"].values]
    ids = [str(v) for v in cube["item_id"].values]

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": cube.shape[1],
        "width": cube.shape[2],
        "count": cube.shape[0],
        "dtype": "float32",
        "crs": cube.attrs["crs"],
        "transform": Affine(*cube.attrs["transform"]),
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": True,
    }
    windows = _cube_windows(cube)
    with rasterio.open(dest, "w", **profile) as dst:
        for band, (stamp, item_id) in enumerate(zip(stamps, ids, strict=True), start=1):
            for (row0, row1), (col0, col1) in windows:
                part = cube.isel(time=band - 1, y=slice(row0, row1), x=slice(col0, col1))
                dst.write(
                    np.asarray(part.values, dtype="float32"),
                    band,
                    window=Window(col0, row0, col1 - col0, row1 - row0),
                )
            dst.set_band_description(band, f"{stamp} {item_id}")
        dst.update_tags(
            item_ids=",".join(ids),
            datetimes=",".join(stamps),
            extent=cube.attrs["extent"],
            # The resolved CRS, so a file built with crs="utm" says which zone.
            crs=cube.attrs["crs"],
            units=cube.attrs["units"],
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
            **_as_geotiff_tags(cube.attrs.get("provenance") or {}),
        )
    return dest
