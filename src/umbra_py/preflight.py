"""Ask a complex product what it can support, without downloading it.

Three of ``umbra convert``'s corrections depend on the product describing
itself: radiometric calibration reads the SICD's ``Radiometric`` scale-factor
polynomials, ``--noise-model measured`` reads its
``Radiometric.NoiseLevel.NoisePoly``, and ``--rtc`` reads the collection
geometry out of its ``SCPCOA`` block. Umbra's open products generally carry
neither of the first two, and refusing is the point — a scaling by an invented
number is indistinguishable in the output from a measured one. The third is
usually present, which is what makes it worth asking about rather than
assuming: the run it refuses is the expensive one (a DEM fetch and a warp on top
of the complex read), and its absence is the case nobody plans for.

:class:`~umbra_py.exceptions.UnsupportedMeasurementError` already made that
refusal *survivable* (``umbra chips --skip-unsupported`` records the pass and
carries on) and cheap *per scene* (the check runs off the metadata the moment
the reader opens, so an uncalibratable product costs its header rather than a
whole complex read). What it could not make cheap is the *discovery*: a SICD's
metadata lives inside the NITF, so finding out still meant one whole-product
download per acquisition. Over a site's twenty passes that is tens of gigabytes
spent to learn that none of them can be calibrated.

This module answers the same question over the wire. A NITF states its own
layout in a fixed-width file header, so the SICD XML — a data extension segment
(DES) near the end of the file — can be located and fetched with two HTTP range
requests and a few tens of kilobytes, off the same anonymous HTTPS the rest of
the library uses. :func:`sicd_capabilities` returns what the product declares;
:func:`preflight_items` asks it of a whole selection and says which passes a
measurement can be made from *before* any of them is downloaded — several
products at a time (:data:`DEFAULT_PREFLIGHT_WORKERS`), because once the answer
costs kilobytes the only thing left that scales with the number of passes is the
round trip, and a check that runs in front of a batch should not be the batch's
slowest part.

The verdict is not a second opinion about what a product supports: the parsed
XML is presented to :mod:`umbra_py.convert`'s own
``_check_measurement_support`` — the same function the conversion runs, calling
the same coefficient readers — so a preflight that says yes and a conversion
that then refuses cannot disagree. What differs is only where the metadata came
from: a range read rather than a downloaded file.

The parse is stdlib + ``requests``: no ``sarpy``, no ``numpy``, no ``[convert]``
extra, so "can this archive answer my question?" is answerable from a core
install. (Confirming a *yes* on a product that does carry the polynomials reads
their coefficients, which needs ``numpy`` — the same dependency the conversion
it clears the way for needs anyway.)
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from ._http import DEFAULT_TIMEOUT, default_session
from .exceptions import UmbraError, UnsupportedMeasurementError
from .models import UmbraItem

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import requests

#: The complex product this reads. ``CPHD`` is phase history rather than a
#: focused image and carries no ``Radiometric`` block to ask about.
PREFLIGHT_ASSET = "SICD"

#: How many acquisitions :func:`preflight_items` reads at once.
#:
#: A preflight spends almost nothing but *waiting*: two small range requests per
#: product, each a round trip to S3, so a serial walk over a site's forty passes
#: is forty round trips of latency in front of a batch whose whole point is not
#: paying for things twice. Reading a few at a time collapses that toward
#: ``N / workers`` without changing a single byte of what is transferred.
#:
#: Eight matches the catalog walk's own sidecar fan-out (``catalog._SIDECAR_WORKERS``)
#: and sits inside the shared session's connection pool, so the concurrency costs
#: no reconnections.
DEFAULT_PREFLIGHT_WORKERS = 8

# --------------------------------------------------------------------------- #
# NITF 2.1 file header (MIL-STD-2500C).
#
# Every field is fixed-width ASCII, so the offsets of the three that matter --
# the header length, the file length, and the segment tables -- are constants.
# The segment tables are what locate the SICD XML: the file is the header,
# then each image / graphic / text segment's subheader and data in order, then
# the data extension segments. Summing the declared lengths gives the byte
# offset of each DES without reading anything between them.
# --------------------------------------------------------------------------- #

_MAGIC = b"NITF"
_VERSION = b"02.10"
#: Offset of ``FL`` (total file length, 12 digits): after ``OPHONE``.
_FL_OFFSET = 342
#: Offset of ``HL`` (header length, 6 digits), then ``NUMI`` (3 digits).
_HL_OFFSET = 354
_NUMI_OFFSET = 360
#: First read: enough for the header of a product with a handful of segments.
#: ``HL`` is read from it and the remainder fetched only if the header is longer.
_HEADER_PROBE = 4096
#: A SICD XML DES is a few hundred kilobytes. Anything vastly larger is not the
#: segment we are looking for, and reading it would defeat the point.
_MAX_XML_BYTES = 32 << 20
#: ``DESID`` values that carry SICD XML. ``XML_DATA_CONTENT`` is what SICD 1.x
#: mandates; the older label is accepted because reading it costs nothing.
_XML_DES_IDS = ("XML_DATA_CONTENT", "SICD_XML")


def _digits(buf: bytes, offset: int, length: int) -> int:
    """Read a fixed-width ASCII integer field out of a NITF header."""
    raw = buf[offset : offset + length]
    if len(raw) < length:
        raise UmbraError(
            f"NITF header ends inside a field at byte {offset}: the file is "
            "truncated or is not a NITF product."
        )
    try:
        return int(raw.decode("ascii").strip() or 0)
    except (UnicodeDecodeError, ValueError) as exc:
        raise UmbraError(
            f"NITF header field at byte {offset} is not a number ({raw!r}), so the "
            "file's segment layout cannot be read."
        ) from exc


def _des_segments(header: bytes) -> tuple[tuple[int, int, int], ...]:
    """Locate every data extension segment as ``(offset, subheader_len, data_len)``.

    Walks the header's segment tables, accumulating each segment's declared
    subheader and data lengths, so a DES's position is arithmetic on ~30 bytes
    of header rather than a scan of the file.
    """
    offset = _digits(header, _HL_OFFSET, 6)
    pos = _NUMI_OFFSET
    # (count field width is always 3; the per-segment subheader/data widths differ)
    for width in ((6, 10), (4, 6)):  # image segments, then graphic segments
        count = _digits(header, pos, 3)
        pos += 3
        sub_w, data_w = width
        for _ in range(count):
            offset += _digits(header, pos, sub_w) + _digits(header, pos + sub_w, data_w)
            pos += sub_w + data_w
    pos += 3  # NUMX: reserved in NITF 2.1, always 000, never a segment
    count = _digits(header, pos, 3)
    pos += 3
    for _ in range(count):  # text segments
        offset += _digits(header, pos, 4) + _digits(header, pos + 4, 5)
        pos += 9
    count = _digits(header, pos, 3)
    pos += 3
    segments = []
    for _ in range(count):
        sub_len = _digits(header, pos, 4)
        data_len = _digits(header, pos + 4, 9)
        pos += 13
        segments.append((offset, sub_len, data_len))
        offset += sub_len + data_len
    return tuple(segments)


class _RangeReader:
    """Read byte ranges out of a SICD product, over HTTPS or from disk.

    Counts what it read (:attr:`bytes_read`) and learns the product's total size
    on the way (:attr:`size`, from the range response's ``Content-Range``), which
    is what lets a preflight report the download it *didn't* do.
    """

    def __init__(self, src: str | os.PathLike, *, session: requests.Session | None = None):
        self.src = str(src)
        self.remote = self.src.startswith(("http://", "https://"))
        self._session = session
        self.bytes_read = 0
        self.size: int | None = None
        if not self.remote:
            try:
                self.size = os.path.getsize(self.src)
            except OSError:
                self.size = None

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        data = (
            self._read_remote(offset, length) if self.remote else self._read_local(offset, length)
        )
        self.bytes_read += len(data)
        return data

    def _read_local(self, offset: int, length: int) -> bytes:
        with open(self.src, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def _read_remote(self, offset: int, length: int) -> bytes:
        session = self._session or default_session()
        self._session = session
        end = offset + length - 1
        resp = session.get(
            self.src,
            headers={"Range": f"bytes={offset}-{end}"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[1].strip()
            if total.isdigit():
                self.size = int(total)
        body = resp.content
        if resp.status_code != 206:
            # A server that ignores Range hands back the whole object; slice it
            # rather than misread the offsets. Rare, and only costs this read.
            body = body[offset : offset + length]
        return body


def read_sicd_xml(
    src: str | os.PathLike,
    *,
    session: requests.Session | None = None,
) -> tuple[str, int, int | None]:
    """Fetch a SICD product's XML metadata without reading its pixels.

    Returns ``(xml, bytes_read, product_bytes)`` — the SICD XML document, how
    many bytes were transferred to get it, and the product's total size when the
    source states one (``Content-Range`` for a URL, the file size for a path).

    ``src`` is an HTTPS URL (read with two range requests) or a local NITF path.
    """
    reader = _RangeReader(src, session=session)
    head = reader.read(0, _HEADER_PROBE)
    if head[:4] != _MAGIC:
        raise UmbraError(
            f"{reader.src} does not start with a NITF file header, so it is not a "
            "SICD product this can read.",
            hint="Pass a SICD asset href (item.asset_href('SICD')) or a local .nitf file.",
        )
    if head[4:9] != _VERSION:
        raise UmbraError(
            f"NITF version {head[4:9].decode('ascii', 'replace')!r} is not 02.10; "
            "only NITF 2.1 (what SICD mandates) has the header layout this reads.",
        )
    header_len = _digits(head, _HL_OFFSET, 6)
    if header_len > len(head):
        head += reader.read(len(head), header_len - len(head))
    if reader.size is None:
        reader.size = _digits(head, _FL_OFFSET, 12) or None

    for offset, sub_len, data_len in _des_segments(head):
        if data_len > _MAX_XML_BYTES:
            continue
        chunk = reader.read(offset, sub_len + data_len)
        desid = chunk[2:27].decode("ascii", "replace").strip()
        if desid not in _XML_DES_IDS:
            continue
        body = chunk[sub_len:]
        if b"<SICD" not in body:
            continue
        return body.decode("utf-8", "replace"), reader.bytes_read, reader.size

    raise UmbraError(
        f"{reader.src} carries no SICD XML data extension segment, so it states "
        "nothing about what it can support.",
        hint="Check the asset is a SICD rather than a GEC/CSI raster.",
    )


# --------------------------------------------------------------------------- #
# An attribute view over the XML, so convert.py's own checks can run on it.
# --------------------------------------------------------------------------- #


def _local_name(tag: str) -> str:
    """The tag without its ``{namespace}`` prefix (SICD XML is namespaced)."""
    return tag.rsplit("}", 1)[-1]


def _scalar(text: str | None):
    """A leaf element's text as the narrowest type it parses as."""
    value = (text or "").strip()
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def _poly_coefficients(element: ElementTree.Element) -> list[list[float]] | None:
    """A SICD polynomial element's ``<Coef exponent1= exponent2=>`` grid.

    SICD writes a 2-D polynomial as a sparse list of exponent-addressed
    coefficients; ``sarpy`` presents the same thing as a dense ``Coefs`` array,
    which is what :mod:`umbra_py.convert` reads. Returning the dense form here is
    what lets the conversion's own coefficient readers run against parsed XML.
    """
    coefs = [child for child in element if _local_name(child.tag) == "Coef"]
    if not coefs:
        return None
    rows = max(int(c.get("exponent1", 0)) for c in coefs) + 1
    cols = max(int(c.get("exponent2", 0)) for c in coefs) + 1
    dense = [[0.0] * cols for _ in range(rows)]
    for coef in coefs:
        dense[int(coef.get("exponent1", 0))][int(coef.get("exponent2", 0))] = float(
            (coef.text or "0").strip()
        )
    return dense


class _SicdView:
    """Attribute access over a SICD XML element, shaped like a sarpy SICD.

    ``sicd.Radiometric.NoiseLevel.NoiseLevelType`` and
    ``sicd.Grid.Row.SS`` read the same whether the metadata came from ``sarpy``
    or from this module's range read, which is the point: the refusals in
    :mod:`umbra_py.convert` are ``getattr`` chains, so they run unchanged here.
    """

    def __init__(self, element: ElementTree.Element) -> None:
        self._element = element

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name == "Coefs":
            dense = _poly_coefficients(self._element)
            if dense is None:
                raise AttributeError(name)
            return dense
        for child in self._element:
            if _local_name(child.tag) == name:
                return _SicdView(child) if len(child) else _scalar(child.text)
        raise AttributeError(name)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<_SicdView {_local_name(self._element.tag)}>"


def parse_sicd_xml(xml: str) -> _SicdView:
    """Parse SICD XML into the attribute view the conversion's checks read."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise UmbraError(f"SICD XML metadata could not be parsed: {exc}") from exc
    return _SicdView(root)


# --------------------------------------------------------------------------- #
# What a product declares, and whether it answers the question being asked.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SicdCapabilities:
    """What one complex product's own metadata says it can support.

    Attributes
    ----------
    source:
        The URL or path the metadata was read from.
    calibrations:
        The subset of :data:`umbra_py.convert.CALIBRATION_TYPES` whose
        scale-factor polynomial this product carries — empty for an uncalibrated
        product, which is most of Umbra's open archive.
    noise_level:
        ``"ABSOLUTE"`` (a floor that can be subtracted), ``"RELATIVE"`` (its
        variation is described but not its level) or ``None``.
    look_geometry:
        ``(incidence_deg, azimuth_deg)`` from the product's ``SCPCOA`` block, or
        ``None`` where it states neither — the scene-centre geometry radiometric
        terrain flattening (``--rtc``) tilts by the DEM's own slope. Unlike the
        two ``Radiometric`` answers this is present on most products, which is
        precisely why it is worth reporting: a *missing* one is the exception,
        and the run it would have refused is an expensive one.
    core_name / rows / cols / polarization:
        Identity, for a report that names the scene rather than only its URL.
    bytes_read:
        Bytes transferred to answer, all of it header and XML.
    product_bytes:
        The whole product's size, when the source stated one — the download this
        did not do.
    """

    source: str
    calibrations: tuple[str, ...]
    noise_level: str | None
    look_geometry: tuple[float, float] | None = None
    core_name: str | None = None
    rows: int | None = None
    cols: int | None = None
    polarization: str | None = None
    bytes_read: int = 0
    product_bytes: int | None = None
    sicd: Any = field(default=None, repr=False, compare=False)

    def refusal(
        self,
        *,
        calibration: str | None = None,
        noise_subtract: bool = False,
        noise_model: str = "measured",
        rtc: bool = False,
    ) -> UnsupportedMeasurementError | None:
        """The refusal a conversion with these settings would raise, or ``None``.

        Runs :mod:`umbra_py.convert`'s own ``_check_measurement_support`` against
        the parsed metadata, so the answer is the conversion's answer rather than
        a restatement of it.
        """
        from .convert import _check_measurement_support  # noqa: PLC0415

        try:
            _check_measurement_support(
                self.sicd,
                calibration=calibration,
                noise_subtract=noise_subtract,
                noise_model=noise_model,
                rtc=rtc,
            )
        except UnsupportedMeasurementError as exc:
            return exc
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form (the parsed metadata view is not included)."""
        return {
            "source": self.source,
            "calibrations": list(self.calibrations),
            "noise_level": self.noise_level,
            "look_geometry": None if self.look_geometry is None else list(self.look_geometry),
            "core_name": self.core_name,
            "rows": self.rows,
            "cols": self.cols,
            "polarization": self.polarization,
            "bytes_read": self.bytes_read,
            "product_bytes": self.product_bytes,
        }


def _declared_calibrations(sicd: Any) -> tuple[str, ...]:
    """Which calibrations the parsed metadata carries, by convert's own map."""
    from .convert import _available_calibrations  # noqa: PLC0415

    return _available_calibrations(sicd)


def _declared_look_geometry(sicd: Any) -> tuple[float, float] | None:
    """The scene-centre ``(incidence, azimuth)`` this product states, if any.

    Read through :mod:`umbra_py.convert`'s own ``_scene_look_geometry`` for the
    same reason the calibrations are read through ``_available_calibrations``:
    what the report says a product carries and what the conversion will accept
    have to be one answer. The refusal is the *absence*, so it is turned back
    into ``None`` here — :meth:`SicdCapabilities.refusal` is where a refusal is
    a refusal.
    """
    from .convert import _scene_look_geometry  # noqa: PLC0415

    try:
        return _scene_look_geometry(sicd)
    except UnsupportedMeasurementError:
        return None


def _optional(sicd: Any, *path: str) -> Any:
    """Walk an attribute path, returning ``None`` at the first missing step."""
    node: Any = sicd
    for name in path:
        node = getattr(node, name, None)
        if node is None:
            return None
    return node


def sicd_capabilities(
    src: str | os.PathLike,
    *,
    session: requests.Session | None = None,
) -> SicdCapabilities:
    """Read a SICD product's metadata over the wire and report what it supports.

    ``src`` is an HTTPS URL or a local NITF path. Only the NITF header and the
    XML data extension segment are read — typically a few tens of kilobytes of a
    multi-gigabyte product — so this is the question to ask *before* deciding
    which acquisitions are worth downloading.
    """
    from .convert import _noise_level_type  # noqa: PLC0415

    xml, bytes_read, product_bytes = read_sicd_xml(src, session=session)
    sicd = parse_sicd_xml(xml)
    polarization = _optional(sicd, "ImageFormation", "TxRcvPolarizationProc")
    return SicdCapabilities(
        source=str(src),
        calibrations=_declared_calibrations(sicd),
        noise_level=_noise_level_type(sicd),
        look_geometry=_declared_look_geometry(sicd),
        core_name=_optional(sicd, "CollectionInfo", "CoreName"),
        rows=_optional(sicd, "ImageData", "NumRows"),
        cols=_optional(sicd, "ImageData", "NumCols"),
        polarization=str(polarization) if polarization is not None else None,
        bytes_read=bytes_read,
        product_bytes=product_bytes,
        sicd=sicd,
    )


@dataclass(frozen=True)
class PreflightResult:
    """Whether one acquisition can support the measurement being planned."""

    item_id: str
    datetime: str | None
    href: str
    supported: bool
    reason: str | None = None
    hint: str | None = None
    capabilities: SicdCapabilities | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "datetime": self.datetime,
            "href": self.href,
            "supported": self.supported,
            "reason": self.reason,
            "hint": self.hint,
            "error": self.error,
            "capabilities": None if self.capabilities is None else self.capabilities.to_dict(),
        }


@dataclass(frozen=True)
class PreflightReport:
    """What a selection of acquisitions can support, and what asking cost.

    ``bytes_read`` against ``product_bytes`` is the whole point of the report:
    the second is what discovering the same thing by conversion would have
    downloaded.
    """

    results: tuple[PreflightResult, ...]
    asset: str
    calibration: str | None
    noise_subtract: bool
    noise_model: str
    rtc: bool = False
    workers: int = 1
    """How many acquisitions were read at once -- the *effective* lane count,
    never more than there were products to read. It says how the answer was
    obtained rather than what it is: the verdicts, their order and the bytes
    read are identical at any width."""

    @property
    def supported(self) -> tuple[PreflightResult, ...]:
        return tuple(r for r in self.results if r.supported)

    @property
    def unsupported(self) -> tuple[PreflightResult, ...]:
        return tuple(r for r in self.results if not r.supported)

    @property
    def bytes_read(self) -> int:
        return sum(r.capabilities.bytes_read for r in self.results if r.capabilities)

    @property
    def product_bytes(self) -> int | None:
        sizes = [
            r.capabilities.product_bytes
            for r in self.results
            if r.capabilities and r.capabilities.product_bytes
        ]
        return sum(sizes) if sizes else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "calibration": self.calibration,
            "noise_subtract": self.noise_subtract,
            "noise_model": self.noise_model,
            "rtc": self.rtc,
            "count": len(self.results),
            "supported_count": len(self.supported),
            "bytes_read": self.bytes_read,
            "product_bytes": self.product_bytes,
            "workers": self.workers,
            "results": [r.to_dict() for r in self.results],
        }


@contextmanager
def _verdicts_in_order(
    check: Callable[[UmbraItem], PreflightResult],
    items: list[UmbraItem],
    lanes: int,
) -> Iterator[Iterator[PreflightResult]]:
    """Yield an iterator of ``check(item)`` results, in ``items`` order.

    The order is the contract, not an accident of the implementation: the chip
    run pairs the verdicts against its own selection positionally
    (``zip(items, report.results, strict=True)``), because two passes of one task
    can share an id and a lookup would collide. So the reads are *issued*
    concurrently and *consumed* in the order they were asked in — every lane is
    busy, and the answer is the serial walk's answer.

    ``lanes == 1`` runs no pool at all, so a one-product preflight (and any
    caller that asks for the serial walk back) is the code that shipped before
    this, thread and all.
    """
    if lanes == 1:
        yield map(check, items)
        return

    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=lanes) as pool:
        # Submitted up front rather than in windows: the pool bounds how many run
        # at once, so there is no barrier where the fast lanes wait on the slowest
        # read of a batch before the next one starts.
        futures = [pool.submit(check, item) for item in items]
        yield (future.result() for future in futures)


def preflight_items(
    items: list[UmbraItem] | tuple[UmbraItem, ...],
    *,
    asset: str = PREFLIGHT_ASSET,
    calibration: str | None = None,
    noise_subtract: bool = False,
    noise_model: str = "measured",
    rtc: bool = False,
    session: requests.Session | None = None,
    progress: Any = None,
    workers: int | None = None,
) -> PreflightReport:
    """Ask a whole selection which passes can support a measurement.

    Reads each acquisition's SICD metadata by range request (see
    :func:`sicd_capabilities`) and applies the conversion's own support check, so
    ``umbra chips --asset SICD --calibrate gamma0`` over the survivors is a run
    with no refusals in it — decided before a single product is downloaded.
    ``rtc=True`` adds the third metadata-dependent correction to the question:
    whether the product states the collection geometry the flattening tilts by
    the terrain's slope.

    An acquisition whose metadata cannot be read at all (a missing asset, an
    unreadable NITF, an HTTP failure) is recorded as unsupported with the error
    on :attr:`PreflightResult.error` rather than ending the walk: a preflight
    that dies on the nineteenth scene has failed at the one thing it is for.

    ``workers`` is how many products are read at once
    (:data:`DEFAULT_PREFLIGHT_WORKERS`; ``None`` takes that default, ``1`` walks
    serially). What the check costs is two range requests per acquisition, so
    what a selection spends is round trips rather than bytes — and that is the
    one part of the preflight that scaled with the number of passes rather than
    with the answer. Reading several at once removes it without moving a single
    verdict: the reads are independent, the shared session's connection pool is
    sized for the fan-out, and the results are consumed in the order they were
    asked in.

    ``progress`` is called ``(index, total, item, result)`` after each
    acquisition, in selection order, from the calling thread — so a CLI progress
    line stays one line per pass in the order they were given, and a callback
    never has to be thread-safe.
    """
    sess = session or default_session()
    results: list[PreflightResult] = []
    items = list(items)
    lanes = max(1, min(DEFAULT_PREFLIGHT_WORKERS if workers is None else int(workers), len(items)))

    def check(item: UmbraItem) -> PreflightResult:
        return _preflight_item(
            item,
            asset=asset,
            calibration=calibration,
            noise_subtract=noise_subtract,
            noise_model=noise_model,
            rtc=rtc,
            session=sess,
        )

    with _verdicts_in_order(check, items, lanes) as verdicts:
        for index, (item, result) in enumerate(zip(items, verdicts, strict=True)):
            results.append(result)
            if progress is not None:
                progress(index + 1, len(items), item, result)
    return PreflightReport(
        results=tuple(results),
        asset=asset,
        calibration=calibration,
        noise_subtract=noise_subtract,
        noise_model=noise_model,
        rtc=rtc,
        workers=lanes,
    )


def _preflight_item(
    item: UmbraItem,
    *,
    asset: str,
    calibration: str | None,
    noise_subtract: bool,
    noise_model: str,
    rtc: bool = False,
    session: requests.Session | None,
) -> PreflightResult:
    """One acquisition's verdict, with every read failure captured rather than raised."""
    when = item.datetime.isoformat() if item.datetime else None
    try:
        href = item.asset_href(asset)
        capabilities = sicd_capabilities(href, session=session)
    except Exception as exc:  # noqa: BLE001 - recorded per item, see preflight_items
        return PreflightResult(
            item_id=item.id,
            datetime=when,
            href="",
            supported=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    refusal = capabilities.refusal(
        calibration=calibration,
        noise_subtract=noise_subtract,
        noise_model=noise_model,
        rtc=rtc,
    )
    return PreflightResult(
        item_id=item.id,
        datetime=when,
        href=href,
        supported=refusal is None,
        reason=None if refusal is None else str(refusal),
        hint=None if refusal is None else refusal.hint,
        capabilities=capabilities,
    )
