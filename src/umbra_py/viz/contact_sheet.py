"""The contact sheet: many acquisitions as one standalone HTML page.

``gallery`` streams a small quicklook per item (in parallel) and lays them out
as a thumbnail grid in a single self-contained page, so you can *browse* a
search result visually before committing to a multi-gigabyte download. Tiles
already baked into the local index by ``umbra index bake-thumbnails`` are
embedded straight from those bytes -- no S3 read, and no ``viz`` extra needed
when every tile is baked. An item that can't be previewed falls back to its
footprint sketch rather than sinking the page.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..models import UmbraItem
from ._deps import _require
from .raster import _png_data_uri, _thumbnail_data_uri


def _render_gallery_thumbnails(
    items: list[UmbraItem],
    *,
    asset: str,
    max_size: int,
    db: bool,
    percentile: tuple[float, float],
    colormap: str | None,
    max_workers: int,
    baked: Mapping[str, bytes] | None = None,
) -> dict[int, str | None]:
    """Build a data-URI thumbnail per item, in parallel.

    Returns ``{index: data_uri_or_None}``. An item whose id is present in
    ``baked`` (a ``{id: PNG bytes}`` map of thumbnails already rendered into the
    index by ``umbra index bake-thumbnails``) is served straight from those bytes
    -- no S3 read, no ``rasterio``, no thread -- so a ``--local`` gallery over a
    baked index is instant and offline. Every other item is streamed the usual
    way: an independent, network-bound cloud-optimized GeoTIFF overview fetched
    via HTTP range requests (which releases the GIL inside GDAL), so a small
    thread pool collapses the wall time of the remaining tiles from N serial
    fetches toward N/workers. Any item that can't be previewed -- no GEC asset,
    decode error, network blip -- maps to ``None`` so its tile falls back to a
    footprint sketch and one bad acquisition never sinks the whole sheet.
    """
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    if not items:
        return {}

    baked = baked or {}
    result: dict[int, str | None] = {}
    to_stream: list[tuple[int, UmbraItem]] = []
    for index, item in enumerate(items):
        png = baked.get(item.id)
        if png is not None:
            result[index] = _png_data_uri(png)
        else:
            to_stream.append((index, item))

    def render(index_item: tuple[int, UmbraItem]) -> tuple[int, str | None]:
        index, item = index_item
        try:
            return index, _thumbnail_data_uri(
                item,
                asset=asset,
                max_size=max_size,
                db=db,
                percentile=percentile,
                colormap=colormap,
            )
        except Exception:
            # A single tile failing must not abort the whole sheet; the tile
            # falls back to its footprint sketch. Mirrors ItemCollection's
            # repr, which likewise never lets one bad thumbnail raise.
            return index, None

    if to_stream:
        workers = max(1, min(max_workers, len(to_stream)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            result.update(dict(pool.map(render, to_stream)))
    return result


def gallery(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 512,
    db: bool = True,
    percentile: tuple[float, float] = (2.0, 98.0),
    colormap: str | None = None,
    max_workers: int = 8,
    title: str = "Umbra SAR gallery",
    subtitle: str | None = None,
    baked: Mapping[str, bytes] | None = None,
) -> str:
    """Render items as a self-contained HTML SAR thumbnail gallery (contact sheet).

    Streams a small SAR quicklook for every item -- only a downsampled overview
    of each cloud-optimized GeoTIFF is fetched via HTTP range requests, never a
    full download -- and lays them out as a thumbnail grid in a single
    standalone HTML page. Each tile links to its STAC item and carries a
    footprint sketch, so you can *browse the catalog visually* before
    committing to a multi-gigabyte download. Thumbnails are fetched in parallel
    (``max_workers``); any item that can't be previewed falls back to its
    footprint sketch rather than failing the page.

    ``db=True`` (the default) uses the radiometrically-correct decibel stretch,
    which reads better at thumbnail size than the linear default. ``asset``
    selects the product to render (``"GEC"``, the detected amplitude GeoTIFF,
    is the sensible default; ``"CSI"`` also works). ``colormap`` names a
    matplotlib colormap for pseudo-colored thumbnails. ``subtitle`` is shown in
    the page header (e.g. the search terms that produced the gallery).

    ``baked`` is an optional ``{id: PNG bytes}`` map of thumbnails already
    rendered into the local index by ``umbra index bake-thumbnails`` (fetched via
    :meth:`umbra_py.index.CatalogIndex.get_thumbnail`). An item found there is
    embedded straight from those bytes -- no S3 read and no ``rasterio`` -- so a
    ``--local`` gallery over a fully-baked index is instant *and* runs in a core
    install; only items missing from ``baked`` are streamed (and only those
    require the ``viz`` extra).

    Returns the HTML as a string; use :func:`save_gallery` to write it to disk.
    Requires the ``viz`` extra (``pip install "umbra-py[viz]"``) unless every
    item is served from ``baked``.
    """
    items = list(items)
    baked = baked or {}

    # rasterio is only needed to *stream* an un-baked thumbnail from S3. When
    # every item is served from the pre-baked index, the whole render is pure
    # standard library, so a core install can produce the gallery. Fail fast
    # only when a stream is actually required -- otherwise every un-baked
    # thumbnail would silently fall back to a footprint and the page would
    # quietly lose its whole point.
    if any(item.id not in baked for item in items):
        _require("rasterio")

    thumbnails = _render_gallery_thumbnails(
        items,
        asset=asset,
        max_size=max_size,
        db=db,
        percentile=percentile,
        colormap=colormap,
        max_workers=max_workers,
        baked=baked,
    )

    from .._html import standalone_gallery_html  # noqa: PLC0415

    return standalone_gallery_html(
        items, thumbnails=thumbnails, title=title, subtitle=subtitle, asset=asset
    )


def save_gallery(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render a SAR gallery and write it to ``dest`` as standalone HTML.

    See :func:`gallery` for the rendering options.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(gallery(items, **kwargs))
    return dest
