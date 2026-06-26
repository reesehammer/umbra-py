"""Exception types raised by umbra-py."""

from __future__ import annotations


class UmbraError(Exception):
    """Base class for all umbra-py errors."""


class CatalogError(UmbraError):
    """Raised when the STAC catalog cannot be read or parsed."""


class AssetNotFoundError(UmbraError):
    """Raised when a requested asset key is not present on an item."""


class DownloadError(UmbraError):
    """Raised when an asset download fails."""


class MissingDependencyError(UmbraError):
    """Raised when an optional dependency (e.g. an extra) is not installed."""


class GeocodeError(UmbraError):
    """Raised when a place name cannot be resolved to a location."""
