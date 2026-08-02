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


class UnsupportedMeasurementError(UmbraError, ValueError):
    """Raised when a product's own metadata cannot support a requested measurement.

    Radiometric calibration needs the SICD's ``Radiometric`` scale-factor
    polynomials, and a ``measured`` noise floor needs its
    ``Radiometric.NoiseLevel.NoisePoly``. Umbra's open products generally carry
    neither, so refusing is the whole point: a scaling or a subtraction by an
    invented number is indistinguishable in the output from a real one. What was
    missing was the *name*. The refusal was a bare :class:`ValueError`, so
    nothing could tell "this product cannot support that" apart from "you asked
    for something that is not a calibration", and a batch had no safe way to
    carry on past a scene whose metadata came up short.

    It is a :class:`ValueError` as well as an :class:`UmbraError` because that is
    what it was before it had a name: every ``except ValueError`` around a
    conversion keeps working, and the new type is something to catch *more*
    narrowly rather than instead.

    It never covers a malformed *request* -- an unknown calibration name, a
    percentile outside the distribution, an even filter window. Those stay bare
    :class:`ValueError`\\ s, because the caller can fix them; this one is a fact
    about the product, and the honest responses to it are a different setting
    (``--noise-model estimated``) or a different scene.
    """


class UnreadableProductError(UmbraError):
    """Raised when what is at an asset href is not a product this can read.

    The sibling of :class:`UnsupportedMeasurementError`, and its complement. That
    one is a product *declaring* it cannot answer — the metadata was read and
    what it says is no. This one is the metadata never being read at all because
    there is nothing there to read: the object the item points at is absent, or
    what is at it is not a NITF, or is a NITF whose header layout this does not
    know, or carries no SICD XML segment, or carries one that does not parse.

    The distinction is the whole reason for the name, because it is the one a
    preflight has to make. A read that fails because the *network* failed says
    nothing about the product — retry it, or keep the pass and let the run find
    out — but a read that fails because the href holds no readable SICD is a
    verdict as final as any refusal: no later download can change it. Before
    this type existed both arrived as the same captured exception, so a batch
    could only take the cautious branch for both and keep a pass it already knew
    would fail.

    It is an :class:`UmbraError` and nothing more specific, so every
    ``except UmbraError`` around a read keeps working and this is something to
    catch *more* narrowly rather than instead. :class:`AssetNotFoundError` is
    deliberately left as itself: "the item lists no such asset" is the same kind
    of fact, found one step earlier and already named.
    """


class GeocodeError(UmbraError):
    """Raised when a place name cannot be resolved to a location."""
