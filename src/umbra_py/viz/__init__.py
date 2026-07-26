"""Visualization helpers for Umbra search results.

This module turns ``UmbraItem`` objects into:

- **GeoJSON features** (zero dependencies) — open them in QGIS, leafmap,
  Earth Engine, geopandas, deck.gl, or anywhere else that reads GeoJSON.
- **Interactive Folium maps** (requires the ``viz`` extra) — drop-in HTML
  for notebooks or sharing, with one polygon per acquisition and a popup
  showing each item's metadata and an "open" link.
- **SAR image overlays** on top of those maps (requires ``viz`` + rasterio):
  ``image_overlay`` and ``footprint_map(..., imagery=True)`` stream a
  downsampled preview of the GEC asset via HTTP range requests and
  composite it onto the basemap. Self-contained — the resulting HTML
  embeds the image as a base64 PNG, no tile server required.
- **Standalone SAR quicklooks** (requires ``viz`` + rasterio):
  ``quicklook`` / ``save_quicklook`` turn one acquisition into a plain
  image file — no map, no GIS, no full download — with optional decibel
  scaling and matplotlib pseudo-color for the radiometrically-correct
  SAR look.
- **Multi-temporal change composites** (requires ``viz`` + rasterio):
  ``change_composite`` / ``save_change_composite`` co-register 2–3
  acquisitions of the same site onto a shared grid and color-code them by
  date, so unchanged ground stays gray while anything that appeared or
  vanished between passes lights up — SAR's signature change-detection
  view, with no manual co-registration.
- **Interactive before/after swipe maps** (requires ``viz`` + rasterio):
  ``swipe_map`` / ``save_swipe_map`` place two passes of the same site on a
  basemap behind a draggable divider, so you wipe one acquisition over the
  other across the same ground — the interactive cousin of a change
  composite.

The first surface is the important one: Umbra acquisitions are points on
the planet, and being able to *see* where a search landed before
downloading multi-gigabyte SAR files is the difference between exploring
the archive and giving up.

Install the optional dependency for the interactive map with::

    pip install "umbra-py[viz]"

Package layout
--------------

This started as one 2 000-line module; it is now five submodules plus the
shared dependency gate, and **every name it ever exported is re-exported
here**, so ``from umbra_py.viz import quicklook`` and ``viz.change_composite``
keep working unchanged:

- :mod:`~umbra_py.viz.geojson` -- items to GeoJSON (no dependencies).
- :mod:`~umbra_py.viz.raster` -- streaming COG reads, stretches, quicklooks,
  thumbnails.
- :mod:`~umbra_py.viz.composites` -- co-registration, change / timescan /
  animation.
- :mod:`~umbra_py.viz.contact_sheet` -- the standalone HTML gallery page (named
  for what it renders rather than ``gallery``, so the submodule cannot be
  shadowed by the :func:`gallery` function re-exported here).
- :mod:`~umbra_py.viz.maps` -- Folium footprint / timeline / swipe maps.
- :mod:`~umbra_py.viz._deps` -- ``_require``, the ``viz``-extra gate.

One consequence worth knowing when writing tests: a submodule binds the
helpers it calls at import time (``from .raster import _stretch_to_rgba``), so
patching an internal helper means patching it on the module that *calls* it
(``umbra_py.viz.maps._stretch_to_rgba``), not on this package. Patching a
*public* function here still works everywhere, because every caller outside
``viz`` resolves it through this namespace at call time.
"""

from __future__ import annotations

from ._deps import (
    _require,
)
from .composites import (
    _compose_change_rgba,
    _compose_timescan_rgba,
    _coregister_bands,
    _label_font,
    _stamp_label,
    _stretch_stat,
    change_animation,
    change_composite,
    save_change_animation,
    save_change_composite,
    save_timescan_composite,
    select_change_frames,
    timescan_composite,
)
from .contact_sheet import (
    _render_gallery_thumbnails,
    gallery,
    save_gallery,
)
from .geojson import (
    _centroid,
    _geometry_for,
    _strip_z,
    _union_bbox,
    item_to_feature,
    items_to_featurecollection,
    write_geojson,
)
from .maps import (
    _ATTRIBUTION_JS,
    _GEOCODE_MIN_INTERVAL,
    _NOMINATIM_REVERSE_URL,
    _SWIPE_SHIM_JS,
    _add_attribution,
    _image_overlay_swipe_shim,
    _install_lazy_imagery,
    _legend_html,
    _popup_html,
    _require_session_for_geocoding,
    _resolve_lazy_urls,
    _reverse_geocode,
    footprint_map,
    save_footprint_map,
    save_swipe_map,
    save_timeline_map,
    swipe_map,
    timeline_map,
)
from .raster import (
    _amplitude_cuts,
    _apply_colormap,
    _normalize_band,
    _png_data_uri,
    _read_sar_band,
    _rgba_overlay,
    _stretch_to_rgba,
    _thumbnail_data_uri,
    _thumbnail_png,
    image_overlay,
    quicklook,
    save_quicklook,
)

__all__ = [
    "change_animation",
    "change_composite",
    "footprint_map",
    "gallery",
    "image_overlay",
    "item_to_feature",
    "items_to_featurecollection",
    "quicklook",
    "save_change_animation",
    "save_change_composite",
    "save_footprint_map",
    "save_gallery",
    "save_quicklook",
    "save_swipe_map",
    "save_timeline_map",
    "save_timescan_composite",
    "select_change_frames",
    "swipe_map",
    "timeline_map",
    "timescan_composite",
    "write_geojson",
]
