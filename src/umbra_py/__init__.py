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
from .download import download_asset, download_item, download_url
from .exceptions import (
    AssetNotFoundError,
    CatalogError,
    DownloadError,
    MissingDependencyError,
    UmbraError,
)
from .models import UmbraItem

__all__ = [
    "__version__",
    "UmbraCatalog",
    "UmbraItem",
    "download_asset",
    "download_item",
    "download_url",
    "PRODUCT_ASSETS",
    "DATA_LICENSE",
    "ATTRIBUTION",
    "UmbraError",
    "CatalogError",
    "AssetNotFoundError",
    "DownloadError",
    "MissingDependencyError",
]
