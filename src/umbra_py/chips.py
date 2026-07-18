"""umbra chips: turn SAR scenes into georeferenced ML training tiles.

For the model-*training* audience, the missing verb is *chipping*: walking a
search result and cutting each scene into fixed-size, georeferenced tiles with a
manifest that carries the metadata a training pipeline needs (look angle,
resolution, polarization, license). This is the data-loading layer for SAR
foundation-model and change-detection research (``docs/AI_INTEGRATION_IDEAS.md``
C4, ``docs/STRATEGY.md`` 5.5) -- the audience most likely to contribute back and
the one that turns free Umbra pixels into demand for Umbra pixels.

Design, following the package's determinism boundary (``docs/AGENTS.md``):

- **No model is called.** Chipping is pure raster iteration + manifest logic;
  it stays in the deterministic core behind the ``[load]`` extra (``rasterio`` +
  ``numpy``), exactly like :mod:`umbra_py.load`, which it mirrors. It reads band
  1 of the item's geocoded GeoTIFF through GDAL's ``/vsicurl/`` driver, so only
  the bytes for each tile are streamed over HTTP range requests -- no
  multi-gigabyte download, and memory stays bounded to one chip at a time.
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

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import ATTRIBUTION, DATA_LICENSE
from .exceptions import AssetNotFoundError
from .load import _open_path, _require
from .models import UmbraItem

#: Product types that are amplitude rasters this chipper can read. The complex
#: ``SICD`` / ``CPHD`` products live in the slant plane and are not display
#: rasters, so chipping them makes no sense; ``SIDD`` is a NITF that GDAL can
#: read but is out of scope for the v1 chipper.
CHIPPABLE_ASSETS = ("GEC", "CSI")

#: Progress callback: ``(item_index, item_total, item, chips_written)``.
ProgressFn = Callable[[int, int, UmbraItem, int], None]


def _safe_slug(text: str) -> str:
    """A filesystem-safe slug for a chip filename stem (keeps names collision-free
    across items while staying readable)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return slug or "item"


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

    @property
    def chip_count(self) -> int:
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        item_ids = sorted({r.item_id for r in self.records})
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
) -> None:
    """Write one chip array as a single-band float32 GeoTIFF with geo + license
    tags, mirroring :func:`umbra_py.load.to_geotiff`'s profile so the chips read
    identically in QGIS / rasterio."""
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
) -> list[ChipRecord]:
    """Cut one acquisition into fixed-size, georeferenced training tiles.

    Reads band 1 of the item's geocoded GeoTIFF (the ``GEC`` cloud-optimized
    GeoTIFF by default) one window at a time via HTTP range requests, and writes
    each full ``chip_size`` x ``chip_size`` tile to ``out_dir`` as a GeoTIFF (or
    a NumPy ``.npy`` array). Returns a :class:`ChipRecord` per written chip.

    Parameters
    ----------
    item:
        The acquisition to chip.
    out_dir:
        Directory to write chips into (created if needed).
    asset:
        Which product to read (``"GEC"`` or ``"CSI"``). The complex
        ``SICD`` / ``CPHD`` products are not amplitude rasters and aren't
        chippable.
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

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(f"Item {item.id!r} has no resolvable URL for asset {asset!r}.")

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stem = prefix or _safe_slug(item.id)
    units = "dB" if db else "amplitude"
    rng, azi = item.resolution
    dt = item.datetime

    records: list[ChipRecord] = []
    with rasterio.open(_open_path(url)) as src:
        nodata = src.nodata
        crs = src.crs
        crs_str = crs.to_string() if crs else None
        rows = range(0, src.height - chip_size + 1, step)
        cols = range(0, src.width - chip_size + 1, step)
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
                        rasterio, chip_path, data, crs_str, tuple(transform)[:6], item, asset, units
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
    manifest: str | None = "manifest.jsonl",
    progress: ProgressFn | None = None,
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
    """
    out_path = Path(out_dir)
    items = list(items)
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
    )
