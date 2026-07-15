"""umbra-py: a Python-first toolkit for Umbra's open SAR data.

Quick start
-----------
>>> from umbra_py import UmbraCatalog
>>> catalog = UmbraCatalog()
>>> for item in catalog.search(start="2024-01-01", end="2024-01-02", limit=5):
...     print(item.summary())
"""

from __future__ import annotations

__version__ = "0.1.0"

from .catalog import UmbraCatalog
from .constants import ATTRIBUTION, DATA_LICENSE, PRODUCT_ASSETS
from .context import llm_context
from .download import download_asset, download_item, download_url
from .exceptions import (
    AssetNotFoundError,
    CatalogError,
    DownloadError,
    GeocodeError,
    MissingDependencyError,
    UmbraError,
)
from .export import export_geoparquet
from .geocode import geocode_place
from .index import CatalogIndex, default_index_path
from .load import to_geotiff, to_xarray
from .models import ItemCollection, UmbraItem
from .viewer import make_viewer_server, view
from .viz import (
    change_animation,
    change_composite,
    footprint_map,
    gallery,
    image_overlay,
    item_to_feature,
    items_to_featurecollection,
    quicklook,
    save_change_animation,
    save_change_composite,
    save_footprint_map,
    save_gallery,
    save_quicklook,
    save_swipe_map,
    save_timeline_map,
    save_timescan_composite,
    select_change_frames,
    swipe_map,
    timeline_map,
    timescan_composite,
    write_geojson,
)

__all__ = [
    "__version__",
    "UmbraCatalog",
    "CatalogIndex",
    "default_index_path",
    "export_geoparquet",
    "UmbraItem",
    "ItemCollection",
    "to_xarray",
    "to_geotiff",
    "download_asset",
    "download_item",
    "download_url",
    "PRODUCT_ASSETS",
    "DATA_LICENSE",
    "ATTRIBUTION",
    "llm_context",
    "UmbraError",
    "CatalogError",
    "AssetNotFoundError",
    "DownloadError",
    "MissingDependencyError",
    "GeocodeError",
    "geocode_place",
    "item_to_feature",
    "items_to_featurecollection",
    "write_geojson",
    "footprint_map",
    "save_footprint_map",
    "image_overlay",
    "quicklook",
    "save_quicklook",
    "change_composite",
    "save_change_composite",
    "timescan_composite",
    "save_timescan_composite",
    "select_change_frames",
    "change_animation",
    "save_change_animation",
    "timeline_map",
    "save_timeline_map",
    "swipe_map",
    "save_swipe_map",
    "gallery",
    "save_gallery",
    "view",
    "make_viewer_server",
]
