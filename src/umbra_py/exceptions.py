"""Exception types raised by umbra-py.

Every error carries an optional ``hint`` -- a single, actionable next step (an
install command, an alternative flag) -- and serializes to a small, stable
dict via :meth:`UmbraError.to_dict`. An agent driving the CLI (or the
``--json`` / ``UMBRA_JSON`` error path in ``cli.main``) can branch on that
structured form instead of pattern-matching a human prose message. The wire
shape is documented in ``docs/schemas/error.schema.json`` and is public API
under the same compatibility rules as everything in ``umbra_py.__all__``.
"""

from __future__ import annotations


class UmbraError(Exception):
    """Base class for all umbra-py errors.

    ``message`` is the human-readable summary (``str(exc)``); ``hint`` is an
    optional, machine-and-human-actionable recovery step. The two are kept
    separate on purpose: the message explains *what* went wrong for a person,
    the hint states *what to do next* in a form an agent can act on verbatim.
    """

    def __init__(self, message: str = "", *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def to_dict(self) -> dict[str, str | None]:
        """Machine-readable form: ``{"error", "message", "hint"}``.

        ``error`` is the exception's class name -- stable within a minor
        version, since the class is part of the public API -- so a caller can
        dispatch on it without parsing the prose ``message``. ``hint`` is
        ``None`` when no recovery step applies.
        """
        return {
            "error": type(self).__name__,
            "message": str(self),
            "hint": self.hint,
        }


class CatalogError(UmbraError):
    """Raised when the STAC catalog cannot be read or parsed."""


class IndexSchemaError(UmbraError):
    """Raised when a local catalog index has an unsupported schema version.

    The on-disk :class:`~umbra_py.CatalogIndex` database records its layout
    version via ``PRAGMA user_version``; opening a database written by a newer
    (unreadable) umbra-py, or by an older versioned schema with no migration
    path, raises this rather than silently misreading the rows.
    """


class AssetNotFoundError(UmbraError):
    """Raised when a requested asset key is not present on an item."""


class DownloadError(UmbraError):
    """Raised when an asset download fails."""


class MissingDependencyError(UmbraError):
    """Raised when an optional dependency (e.g. an extra) is not installed."""


class GeocodeError(UmbraError):
    """Raised when a place name cannot be resolved to a location."""
