"""Search Umbra's published open SAR data.

Umbra publishes each acquisition under one of two public prefixes:

- ``sar-data/tasks/<task>/[<uuid>/]<acquisition>/`` — named campaigns
  (``Centerfield, Utah``, …)
- ``sar-data/task-data/<task-id>/<acquisition>/`` — UUID-keyed collects
  (the bulk of the open archive, including CPHD used for formation
  elsewhere)

each with a ``*.stac.v2.json`` sidecar next to the binary products. Older
(2023 to 2024) collects under ``tasks/`` carry only a legacy
``<stem>_METADATA.json`` sidecar, sometimes with the products laid out flat in a
site folder (``tasks/ad hoc/<site>/<stem>.tif``) instead of an acquisition
directory; the walker indexes those too (see :func:`_acquisition_group` and
:func:`stac_from_legacy_metadata`). The legacy ``stac/`` tree of
``catalog.json`` files lists thousands of items, but most reference data that
was never actually published — searching it returns items whose download URLs
don't resolve.

:class:`UmbraCatalog` walks both live prefixes via paginated S3 listings
(named ``tasks/`` first, then ``task-data/``). Acquisition directory names
start with the acquisition date (``YYYY-MM-DD-HH-MM-SS_PLATFORM``), so a
search bounded by ``start`` / ``end`` prunes whole subtrees without
fetching them. ``task-data/`` is thousands of UUID directories; prefer
``CatalogIndex`` / ``umbra search --local`` for a repeat query.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import defusedxml.ElementTree as _defused_et
import requests
from defusedxml.common import DefusedXmlException

from ._geometry import Geometry
from ._geometry import to_geojson as _geometry_to_geojson
from ._http import default_session, get_json
from .constants import CANOPY_ARCHIVE_URL, S3_BUCKET, S3_REGION
from .dates import parse_date_bound
from .exceptions import CatalogError
from .fuzzy import task_matches
from .models import BBox, UmbraItem

DateLike = str | date | datetime | None

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_TASKS_PREFIX = "sar-data/tasks/"
#: UUID-keyed sibling of :data:`_TASKS_PREFIX`. Same bucket, different tree;
#: a collect published only here is invisible if the walker lists ``tasks/``
#: alone. Named tasks are listed first so a ``limit=1`` search still lands
#: in the small named tree.
_TASK_DATA_PREFIX = "sar-data/task-data/"
#: A third, smaller UUID-keyed root (``open-data/<task-id>/<acquisition>/``,
#: v2 sidecars, Dec 2025 onward). Some of its collects are also under
#: ``task-data/``; the rest are published nowhere else.
_OPEN_DATA_PREFIX = "open-data/"
_DATA_PREFIXES = (_TASKS_PREFIX, _TASK_DATA_PREFIX, _OPEN_DATA_PREFIX)
# Acquisition directories look like 2025-12-06-07-52-28_UMBRA-10/. We use the
# leading YYYY-MM-DD both to identify the acquisition component of a key and
# to prune by date.
_ACQ_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")
#: A full acquisition stem (``2023-07-18-02-30-32_UMBRA-04``) at the start of a
#: file name. The flat legacy layout has no acquisition directory, so the stem
#: in the file name is what groups ``<stem>.tif`` with ``<stem>_SICD.nitf``.
_ACQ_STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_[A-Za-z0-9-]+?)(?=[_.])")
#: The current per-acquisition STAC sidecar.
_V2_SIDECAR_SUFFIX = ".stac.v2.json"
#: The legacy (2023 to 2024) per-acquisition metadata sidecar. Not STAC; mapped
#: to a STAC item by :func:`stac_from_legacy_metadata`.
_LEGACY_SIDECAR_SUFFIX = "_METADATA.json"
#: Some named tasks publish the v2 sidecar in a *sibling* directory named
#: ``<stem>.stac.v2/`` (holding ``<stem>.stac.v2.stac.v2.json``) instead of next
#: to the products in ``<stem>/``. Folding that suffix off the directory name
#: puts the sidecar back in its acquisition's group.
_MISPLACED_V2_DIR_SUFFIX = ".stac.v2"
#: Namespace for the deterministic ids minted for legacy collects, which carry
#: a collect id but no STAC item id. uuid5 over the collect id keeps the id
#: stable across rebuilds and identical for a collect mirrored in two folders.
_LEGACY_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://umbra-py.space/legacy-collect")

# How many acquisition sidecars to fetch concurrently within one task. The
# per-acquisition ``*.stac.v2.json`` GET is the one round trip in an otherwise
# single-LIST task walk, and each is an independent, latency-bound HTTPS request,
# so a small thread pool collapses a task's wall time from N serial fetches
# toward N/workers. We fetch in windows of this size and yield each window in
# date order, so output stays deterministic and an early ``limit`` /
# ``max_per_task`` stop wastes at most one window of fetches.
_SIDECAR_WORKERS = 8

_GEOTIFF_MEDIA = "image/tiff; application=geotiff; profile=cloud-optimized"
_NITF_MEDIA = "application/vnd.nitf"
_JSON_MEDIA = "application/json"
_OCTET_MEDIA = "application/octet-stream"


def _coerce_date(value: DateLike, *, is_end: bool = False) -> date | None:
    """Resolve a search date bound to a concrete :class:`date`.

    Accepts ``date`` / ``datetime`` objects and, for strings, the full
    natural-language grammar in :func:`umbra_py.dates.parse_date_bound` (ISO
    dates, bare years/months, ``today``/``yesterday``, ``"3 months ago"``,
    ``"last month"``, ...). ``is_end`` snaps span expressions (a bare year,
    year-month, or period keyword) to their last day rather than their first,
    so an ``end`` bound covers the whole named period.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return parse_date_bound(value, is_end=is_end)


def _acq_date(prefix: str) -> date | None:
    """Parse the acquisition date from a directory name like
    ``2025-12-06-07-52-28_UMBRA-10/`` (returns ``None`` for anything else)."""
    name = prefix.rstrip("/").rsplit("/", 1)[-1]
    m = _ACQ_DATE_RE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _task_name(task_prefix: str) -> str:
    """Task directory name from a ``sar-data/tasks/<name>/`` or
    ``sar-data/task-data/<id>/`` prefix.

    Named campaigns keep the human label (``"Centerfield, Utah"``); UUID
    collects keep the task id. S3 keys are unencoded, so a named task
    carries its literal spaces / commas.
    """
    for prefix in _DATA_PREFIXES:
        if task_prefix.startswith(prefix):
            return task_prefix[len(prefix) :].rstrip("/")
    return task_prefix.rstrip("/")


def _acquisition_group(rel: str) -> tuple[str, str] | None:
    """Group a key (relative to its task prefix) into its acquisition.

    Returns ``(group, stem)``, where ``group`` is a task-relative id shared by
    every file of one acquisition and ``stem`` is its
    ``YYYY-MM-DD-HH-MM-SS_PLATFORM`` name, or ``None`` for a key that belongs
    to no acquisition (stray bucket files). Three layouts are recognised:

    - ``[<uuid>/]<stem>/<file>``: the acquisition directory is the first path
      segment that starts with a date. A sibling ``<stem>.stac.v2/`` directory
      (a misplaced v2 sidecar) folds into ``<stem>/``.
    - ``<site>/[<uuid>/]<stem>[_PRODUCT].<ext>``: the flat legacy layout, with
      no acquisition directory; the stem in the file name groups the files.
    """
    parts = rel.split("/")
    for i, seg in enumerate(parts[:-1]):
        if _ACQ_DATE_RE.match(seg):
            name = (
                seg[: -len(_MISPLACED_V2_DIR_SUFFIX)]
                if seg.endswith(_MISPLACED_V2_DIR_SUFFIX)
                else seg
            )
            return "/".join([*parts[:i], name]) + "/", name
    m = _ACQ_STEM_RE.match(parts[-1])
    if m:
        stem = m.group(1)
        return "/".join([*parts[:-1], stem]), stem
    return None


def _pick_sidecar(keys: list[str]) -> str | None:
    """The sidecar to build an acquisition's item from: v2 STAC first, then the
    legacy ``_METADATA.json``; ``None`` when the acquisition has neither."""
    v2 = next((k for k in keys if k.endswith(_V2_SIDECAR_SUFFIX)), None)
    if v2 is not None:
        return v2
    return next((k for k in keys if k.endswith(_LEGACY_SIDECAR_SUFFIX)), None)


def _drop_z(geometry: dict[str, Any] | None) -> dict[str, Any] | None:
    """A GeoJSON polygon with any third (height) coordinate removed."""
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon":
        return None
    rings = geometry.get("coordinates") or []
    out = [[[float(pt[0]), float(pt[1])] for pt in ring if len(pt) >= 2] for ring in rings]
    if not out or not out[0]:
        return None
    return {"type": "Polygon", "coordinates": out}


def _lower(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) else None


def stac_from_legacy_metadata(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Map a legacy ``*_METADATA.json`` document to a STAC item dict.

    The 2023 to 2024 collects under ``sar-data/tasks/`` publish this Umbra
    metadata format (``version`` 1.0.0, 1.1.0 or 2.0.0, all sharing the fields
    read here) instead of a ``*.stac.v2.json`` sidecar. The mapping fills the
    same STAC properties the v2 sidecars carry so every search filter
    (date, footprint, polarization, incidence, resolution) treats legacy and v2
    items alike. Incidence is ``angleIncidenceDegrees``, not the grazing angle.

    The item id is a uuid5 of the collect id, since the legacy format has no
    STAC item id; ``umbra:collect_id`` is kept so a legacy item links to any
    reprocessed ``task-data/`` copy of the same collect. Returns ``None`` when
    the document has no collect, start time or footprint.
    """
    collects = doc.get("collects") or []
    if not collects or not isinstance(collects[0], dict):
        return None
    c = collects[0]
    start = c.get("startAtUTC")
    geometry = _drop_z(c.get("footprintPolygonLla"))
    collect_id = c.get("id")
    if not start or geometry is None or not collect_id:
        return None
    ring = geometry["coordinates"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    sat = doc.get("umbraSatelliteName")
    platform = sat.replace("_", "-").title() if isinstance(sat, str) else None
    gec = ((doc.get("derivedProducts") or {}).get("GEC") or [{}])[0] or {}
    res = gec.get("groundResolution") or c.get("maxGroundResolution") or {}
    freq = c.get("radarCenterFrequencyHz")
    props: dict[str, Any] = {
        "datetime": start,
        "start_datetime": start,
        "end_datetime": c.get("endAtUTC"),
        "platform": platform,
        "constellation": "umbra",
        "sar:instrument_mode": doc.get("imagingMode"),
        "sar:frequency_band": c.get("radarBand"),
        "sar:center_frequency": freq / 1e9 if isinstance(freq, (int, float)) else None,
        "sar:polarizations": list(c.get("polarizations") or []),
        "sar:observation_direction": _lower(c.get("observationDirection")),
        "sar:product_type": "GEC",
        "sar:resolution_range": res.get("rangeMeters"),
        "sar:resolution_azimuth": res.get("azimuthMeters"),
        "sat:orbit_state": _lower(c.get("satelliteTrack")),
        "view:incidence_angle": c.get("angleIncidenceDegrees"),
        "view:azimuth": c.get("angleAzimuthDegrees"),
        "umbra:collect_id": collect_id,
        "umbra:task_id": c.get("taskId"),
        "umbra:grazing_angle_degrees": c.get("angleGrazingDegrees"),
        "umbra:slant_range_meters": c.get("slantRangeMeters"),
        "umbra:product_sku": doc.get("productSku"),
        "umbra:legacy_metadata_version": doc.get("version"),
    }
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": str(uuid.uuid5(_LEGACY_ID_NAMESPACE, str(collect_id))),
        "collection": "umbra-sar",
        "geometry": geometry,
        "bbox": [min(lons), min(lats), max(lons), max(lats)],
        "properties": {k: v for k, v in props.items() if v is not None},
        "links": [],
    }


def _datetime_interval(start: date | None, end: date | None) -> str | None:
    """Build an RFC 3339 interval string for a STAC API ``datetime`` filter.

    A closed interval is ``"<start>/<end>"``; an open bound uses ``".."`` (the
    STAC API convention). The start snaps to the first instant of the day and
    the end to the last, so a whole-day ``start``/``end`` bound is inclusive on
    both sides -- matching the inclusive semantics of the open-bucket walk.
    Returns ``None`` when neither bound is set.
    """
    if start is None and end is None:
        return None
    lo = f"{start.isoformat()}T00:00:00Z" if start else ".."
    hi = f"{end.isoformat()}T23:59:59Z" if end else ".."
    return f"{lo}/{hi}"


def _next_link(links: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the STAC API ``rel="next"`` pagination link, if any."""
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            return link
    return None


def _guess_media_type(basename: str) -> str:
    ext = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
    if ext in ("tif", "tiff"):
        return _GEOTIFF_MEDIA
    if ext == "nitf":
        return _NITF_MEDIA
    if ext == "json":
        return _JSON_MEDIA
    return _OCTET_MEDIA


class UmbraCatalog:
    """Client for searching Umbra SAR data.

    By default this searches Umbra's **open** data by crawling the public S3
    bucket (a static STAC catalog with no search API). Pass a Canopy ``token``
    and the *same* :meth:`search` interface instead queries Umbra's
    authenticated **commercial** archive over its real STAC API
    (:data:`~umbra_py.constants.CANOPY_ARCHIVE_URL`)::

        # open data (default) -- no account needed
        UmbraCatalog().search(area="Centerfield", limit=5)

        # commercial archive -- same call, one extra argument
        UmbraCatalog(token="...").search(bbox=bbox, start="2024", limit=5)

    Both paths yield :class:`~umbra_py.UmbraItem` objects, so every downstream
    verb (download, quicklook, change, chips, ...) works unchanged against
    either archive. That is the funnel made literal: a user onboarded on the
    free bucket is already holding the tool they'd use as a paying customer.
    Get a token from https://docs.canopy.umbra.space/.
    """

    def __init__(
        self,
        bucket: str = S3_BUCKET,
        region: str = S3_REGION,
        session: requests.Session | None = None,
        *,
        token: str | None = None,
        archive_url: str = CANOPY_ARCHIVE_URL,
        collections: list[str] | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.session = session or default_session()
        self._list_base = f"https://s3.{region}.amazonaws.com/{bucket}"
        #: When set, :meth:`search` queries the Canopy commercial STAC API
        #: instead of walking the open bucket. Never sent to the open bucket.
        self.token = token
        self.archive_url = archive_url
        #: Optional STAC collection ids to scope a Canopy ``/search`` to.
        self.collections = collections

    # -- HTTP helpers ----------------------------------------------------------

    def _get(self, url: str) -> dict:
        try:
            return get_json(url, session=self.session)
        except requests.RequestException as exc:
            raise CatalogError(f"Failed to read catalog document {url!r}: {exc}") from exc

    @staticmethod
    def _parse_listing(content: bytes) -> ET.Element:
        """Parse an S3 ``ListObjectsV2`` response body defensively.

        The bucket listing is remote input parsed on the library's core
        discovery path, and the listing base is configurable (a caller may
        point the catalog at an arbitrary host), so the response is untrusted.
        Parsing it with :mod:`defusedxml` rejects DTDs, internal entity
        expansion, and external-entity references outright -- closing the
        billion-laughs / XXE class the stdlib ``xml.etree`` parser is exposed
        to -- and turns any such payload, or malformed XML, into a clean
        :class:`CatalogError` instead of resource exhaustion or a raw parse
        traceback.
        """
        try:
            return _defused_et.fromstring(content, forbid_dtd=True)
        except DefusedXmlException as exc:
            raise CatalogError(f"Refused to parse unsafe bucket-listing XML: {exc}") from exc
        except ET.ParseError as exc:
            raise CatalogError(f"Malformed bucket-listing XML: {exc}") from exc

    def _list_prefix(self, prefix: str) -> tuple[list[str], list[str]]:
        """List one level under ``prefix``; return ``(subdirs, files)``.

        ``subdirs`` are the immediate child prefixes (each ending with
        ``/``); ``files`` are full object keys directly under ``prefix``.
        Paginated transparently.
        """
        subdirs: list[str] = []
        files: list[str] = []
        token: str | None = None
        while True:
            # ``list-type=2`` selects the ListObjectsV2 API. Without it S3
            # falls back to V1, which ignores ``continuation-token`` and never
            # returns ``NextContinuationToken`` -- so listings would silently
            # truncate at the first 1,000 keys.
            url = f"{self._list_base}/?list-type=2&prefix={quote(prefix)}&delimiter=/"
            if token:
                url += f"&continuation-token={quote(token)}"
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise CatalogError(f"Failed to list bucket prefix {url!r}: {exc}") from exc
            root = self._parse_listing(resp.content)
            for cp in root.findall(f"{_S3_NS}CommonPrefixes"):
                p = cp.findtext(f"{_S3_NS}Prefix")
                if p:
                    subdirs.append(p)
            for c in root.findall(f"{_S3_NS}Contents"):
                k = c.findtext(f"{_S3_NS}Key")
                if k:
                    files.append(k)
            if root.findtext(f"{_S3_NS}IsTruncated") != "true":
                break
            token = root.findtext(f"{_S3_NS}NextContinuationToken")
            if not token:
                break
        return subdirs, files

    def _stream_keys(self, prefix: str) -> Iterator[str]:
        """Yield every object key under ``prefix`` (no delimiter), paginated.

        Used to enumerate a whole task in a single paginated stream rather
        than one S3 LIST per acquisition directory -- the latter is
        prohibitively slow against the real bucket (~1000s of round
        trips for an unconstrained search).
        """
        token: str | None = None
        while True:
            # ``list-type=2`` selects ListObjectsV2 so ``continuation-token``
            # is honored and ``NextContinuationToken`` is returned; without it
            # a task with >1,000 keys is silently truncated to its first page.
            url = f"{self._list_base}/?list-type=2&prefix={quote(prefix)}"
            if token:
                url += f"&continuation-token={quote(token)}"
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise CatalogError(f"Failed to list bucket prefix {url!r}: {exc}") from exc
            root = self._parse_listing(resp.content)
            for c in root.findall(f"{_S3_NS}Contents"):
                k = c.findtext(f"{_S3_NS}Key")
                if k:
                    yield k
            if root.findtext(f"{_S3_NS}IsTruncated") != "true":
                break
            token = root.findtext(f"{_S3_NS}NextContinuationToken")
            if not token:
                break

    # -- search ----------------------------------------------------------------

    def search(
        self,
        *,
        bbox: BBox | None = None,
        intersects: Geometry | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        area: str | None = None,
        fuzzy: bool = False,
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
        limit: int | None = None,
        max_per_task: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Yield items matching the filters.

        Parameters
        ----------
        bbox:
            ``(min_lon, min_lat, max_lon, max_lat)`` footprint filter.
        intersects:
            A polygon geometry (the exterior-ring form from
            :func:`umbra_py._geometry.parse_geometry`); keep only items whose
            footprint intersects it. A tighter spatial filter than the
            rectangular ``bbox`` -- the standard STAC ``intersects``. Combines
            with ``bbox`` (both must match) when both are given.
        start, end:
            Inclusive acquisition-date bounds. Accepts ``date`` /
            ``datetime`` objects or ISO ``YYYY-MM-DD`` strings. The walker
            still has to list each task to discover what's published in
            range. Named ``tasks/`` is tens of directories; ``task-data/``
            is thousands of UUID directories, so an unconstrained live
            walk is slow -- provide ``limit`` to stop as soon as you have
            enough, and prefer a local index for repeats.
        product_types:
            Keep only items exposing at least one of these assets
            (e.g. ``["GEC"]``).
        area:
            Case-insensitive substring matched against each
            ``sar-data/tasks/<task>/`` or ``sar-data/task-data/<id>/``
            directory name. Named campaigns (e.g. ``"Centerfield, Utah"``)
            live under ``tasks/``; UUID collects live under ``task-data/``.
            ``area="centerfield"`` returns just that named site's
            acquisitions. Non-matching task directories are skipped
            *before* they're listed, so this also makes the search much
            faster -- the ergonomic way to gather the co-located passes a
            change composite needs. Place / bbox search is what finds a
            UUID collect whose directory name is not a place.
        fuzzy:
            Widen ``area`` from a literal substring to a deterministic
            token-wise fuzzy match (:func:`umbra_py.fuzzy.task_matches`):
            word-order- and punctuation-independent, tolerant of a small
            typo, and a strict superset of the substring match (it never
            drops a result). So ``area="utah centerfield"`` or
            ``area="centrfield"`` still reaches ``"Centerfield, Utah"``.
            No model call -- the C1 deterministic first step.
        polarizations:
            Keep only items exposing at least one of these polarizations
            (case-insensitive, e.g. ``["VV"]``) -- the SAR-native filter that
            keeps a change comparison like-with-like (HH and VV image different
            physics). An item with no polarization metadata is excluded.
        min_incidence, max_incidence:
            Inclusive bounds (degrees) on the view incidence angle
            (:attr:`UmbraItem.incidence_angle`). An item with no incidence
            metadata is excluded when either bound is set.
        max_resolution:
            Keep only items at least this fine -- both range and azimuth
            resolution ``<= max_resolution`` metres. An item missing either
            resolution value is excluded. See :meth:`UmbraItem.matches_filters`
            for the exact acquisition-property semantics (a set filter excludes
            items lacking that property, matching the STAC Query extension).
        limit:
            Stop after yielding this many items.
        max_per_task:
            Cap the number of items yielded from any one
            ``sar-data/tasks/<task>/`` or ``sar-data/task-data/<id>/``
            directory. Each task is a tasking campaign over the same
            area, so ``max_per_task=1`` swaps the usual "every revisit of
            a few sites" output for "one acquisition per distinct site"
            -- much better diversity on a map.

        Notes
        -----
        When this catalog was created with a Canopy ``token``, the search runs
        against Umbra's commercial STAC API instead of the open bucket. The
        filters mean the same thing; ``bbox`` and the date bounds are sent to
        the API, while ``product_types`` and ``area``/``fuzzy`` are applied to
        the returned items (exactly as they are on the open-bucket path), so the
        interface is identical across both archives.
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end, is_end=True)
        wanted = {p.upper() for p in product_types} if product_types else None
        acq_filters: dict[str, Any] = {
            "polarizations": polarizations,
            "min_incidence": min_incidence,
            "max_incidence": max_incidence,
            "max_resolution": max_resolution,
        }

        if self.token:
            yield from self._search_archive(
                bbox=bbox,
                intersects=intersects,
                start=start_d,
                end=end_d,
                wanted=wanted,
                area=area,
                fuzzy=fuzzy,
                acq_filters=acq_filters,
                limit=limit,
                max_per_task=max_per_task,
            )
            return

        task_subdirs: list[str] = []
        for data_prefix in _DATA_PREFIXES:
            subdirs, _ = self._list_prefix(data_prefix)
            task_subdirs.extend(subdirs)
        if area:
            task_subdirs = [
                t for t in task_subdirs if task_matches(area, _task_name(t), fuzzy=fuzzy)
            ]

        count = 0
        for task_prefix in task_subdirs:
            per_task = 0
            for item in self._walk_task(task_prefix, start_d, end_d):
                if bbox is not None and not item.intersects_bbox(bbox):
                    continue
                if intersects is not None and not item.intersects_polygon(intersects):
                    continue
                if wanted is not None and not (wanted & set(item.available_assets)):
                    continue
                if not item.matches_filters(**acq_filters):
                    continue
                yield item
                count += 1
                per_task += 1
                if limit is not None and count >= limit:
                    return
                if max_per_task is not None and per_task >= max_per_task:
                    break

    # -- commercial archive (Canopy STAC API) ----------------------------------

    def _search_archive(
        self,
        *,
        bbox: BBox | None,
        intersects: Geometry | None,
        start: date | None,
        end: date | None,
        wanted: set[str] | None,
        area: str | None,
        fuzzy: bool,
        acq_filters: dict[str, Any],
        limit: int | None,
        max_per_task: int | None,
    ) -> Iterator[UmbraItem]:
        """Search the Canopy commercial archive over its STAC API.

        Umbra's commercial product *does* expose a real STAC API, so unlike the
        open bucket we POST a standard STAC item-search body and follow the
        ``rel="next"`` pagination links, building an :class:`UmbraItem` from each
        returned feature (whose asset hrefs are already resolvable URLs, so no
        rewrite is needed). ``bbox`` and the date interval are pushed down to the
        API; ``product_types``, ``area``/``fuzzy`` and the acquisition-property
        filters (``acq_filters``: polarizations / incidence / resolution) are
        applied client-side to keep exact parity with the open-bucket walk.
        """
        body: dict[str, Any] = {}
        if self.collections:
            body["collections"] = list(self.collections)
        if intersects is not None:
            # The STAC API can filter by geometry itself; send the polygon and
            # still re-check each returned footprint client-side (below) so a
            # server that ignores or loosens the filter can't leak non-matches.
            geojson = _geometry_to_geojson(intersects)
            if geojson is not None:
                body["intersects"] = geojson
        elif bbox is not None:
            body["bbox"] = list(bbox)
        interval = _datetime_interval(start, end)
        if interval:
            body["datetime"] = interval
        # A page size: request no more than we need, capped so a huge/unbounded
        # limit doesn't ask a server for an unreasonable page.
        body["limit"] = min(limit, 500) if limit else 100

        url: str | None = self.archive_url
        method = "POST"
        next_body: dict[str, Any] | None = body
        count = 0
        per_task: dict[str | None, int] = {}
        while url is not None:
            page = self._archive_page(url, method, next_body)
            for feature in page.get("features", []):
                item = UmbraItem.from_dict(feature)
                if intersects is not None and not item.intersects_polygon(intersects):
                    continue
                if wanted is not None and not (wanted & set(item.available_assets)):
                    continue
                if area and not task_matches(area, item.task or "", fuzzy=fuzzy):
                    continue
                if not item.matches_filters(**acq_filters):
                    continue
                if max_per_task is not None:
                    seen = per_task.get(item.task, 0)
                    if seen >= max_per_task:
                        continue
                    per_task[item.task] = seen + 1
                yield item
                count += 1
                if limit is not None and count >= limit:
                    return
            nxt = _next_link(page.get("links", []))
            if nxt is None:
                break
            url = nxt["href"]
            method = str(nxt.get("method", "GET")).upper()
            if method == "POST":
                page_body = nxt.get("body") or {}
                # STAC API next links may ask the client to merge the extra body
                # into the original request or replace it wholesale.
                next_body = {**(next_body or {}), **page_body} if nxt.get("merge") else page_body
            else:
                next_body = None

    def get_item(self, item_id: str) -> UmbraItem | None:
        """Fetch a single acquisition from the Canopy commercial archive by id.

        The keyed-retrieval complement to :meth:`search`'s listing: given a STAC
        item id, return that one :class:`~umbra_py.UmbraItem`, or ``None`` when the
        archive has no such item. It is implemented with the STAC API ``ids``
        search extension over the *same* ``/archive/search`` endpoint
        :meth:`search` already POSTs to -- ``POST {"ids": [item_id], "limit": 1}``
        -- so it introduces no new endpoint to guess and stays offline-testable
        against a mocked API, exactly like the search path. Bearer auth, the
        helpful ``401/403`` "token rejected" message and the ``500`` wrap are all
        inherited from :meth:`_archive_page`.

        Requires a Canopy ``token``. The open bucket is a *static* catalog with no
        id-to-item index, so a keyed lookup isn't meaningful there -- resolve an
        open-data item from its sidecar URL instead
        (:meth:`UmbraItem.from_dict` / ``umbra info <url>``) or from a built index
        (:meth:`umbra_py.CatalogIndex.get`).
        """
        if not self.token:
            raise CatalogError(
                "get_item(id) queries the Canopy commercial archive and needs a "
                "token (UmbraCatalog(token=...) or the UMBRA_CANOPY_TOKEN "
                "environment variable). For the open data, read a sidecar URL with "
                "UmbraItem.from_dict / 'umbra info <url>', or look an item up in a "
                "built index with CatalogIndex.get(item_id)."
            )
        body: dict[str, Any] = {"ids": [item_id], "limit": 1}
        if self.collections:
            body["collections"] = list(self.collections)
        page = self._archive_page(self.archive_url, "POST", body)
        for feature in page.get("features", []):
            item = UmbraItem.from_dict(feature)
            # Guard against a server that ignores the ``ids`` filter and returns
            # an unrelated page: only accept the exact id we asked for.
            if item.id == item_id:
                return item
        return None

    def _archive_page(self, url: str, method: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Fetch one page from the Canopy STAC API with bearer auth."""
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            if method == "POST":
                resp = self.session.post(url, json=body or {}, headers=headers, timeout=30)
            else:
                resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (401, 403):
                raise CatalogError(
                    "Canopy archive rejected the token (HTTP "
                    f"{status}). Check the token passed to UmbraCatalog(token=...) "
                    "or the UMBRA_CANOPY_TOKEN environment variable."
                ) from exc
            raise CatalogError(f"Canopy archive search failed ({url!r}): {exc}") from exc
        except requests.RequestException as exc:
            raise CatalogError(f"Canopy archive search failed ({url!r}): {exc}") from exc
        try:
            return resp.json()
        except ValueError as exc:
            raise CatalogError(
                f"Canopy archive returned a non-JSON response from {url!r}."
            ) from exc

    def _walk_task(
        self,
        task_prefix: str,
        start: date | None,
        end: date | None,
    ) -> Iterator[UmbraItem]:
        """Stream every key under one task and yield in-range acquisitions.

        Tasks are organised as either ``<task>/<acquisition>/<file>``
        (UUID-style tasks) or ``<task>/<inner-uuid>/<acquisition>/<file>``
        (named tasks). We don't know which up front and we can't usefully
        prefix-prune by date for named tasks (inner UUIDs sort randomly),
        so we do one paginated non-delimited listing per task, identify
        the acquisition component by its ``YYYY-MM-DD-HH-MM-SS`` prefix,
        and group files by acquisition directory client-side.
        """
        by_acq: dict[str, list[str]] = {}
        for key in self._stream_keys(task_prefix):
            # The acquisition is the first date-named directory, or (flat
            # legacy layout) the stem of the file name; skip anything with
            # neither (stray bucket junk). See _acquisition_group.
            group = _acquisition_group(key[len(task_prefix) :])
            if group is None:
                continue
            d = _acq_date(group[1])
            if start is not None and d is not None and d < start:
                continue
            if end is not None and d is not None and d > end:
                continue
            by_acq.setdefault(task_prefix + group[0], []).append(key)

        # Collect the acquisitions that have a sidecar (v2 STAC, else legacy
        # _METADATA.json), sorted so output order is deterministic (older
        # acquisitions first). Each still needs one sidecar GET -- the N+1
        # round trips in an otherwise single-LIST walk -- which
        # _items_from_sidecars resolves concurrently while preserving order.
        pending: list[tuple[str, list[str], str]] = []
        for acq_prefix in sorted(by_acq):
            keys = by_acq[acq_prefix]
            sidecar = _pick_sidecar(keys)
            if sidecar is None:
                continue
            pending.append((acq_prefix, keys, self._url_for(sidecar)))
        yield from self._items_from_sidecars(pending)

    def _items_from_sidecars(
        self, pending: list[tuple[str, list[str], str]]
    ) -> Iterator[UmbraItem]:
        """Fetch each acquisition's sidecar and build its item, order-preserving.

        ``pending`` is ``(acq_prefix, keys, sidecar_url)`` tuples already in the
        date order the walk yields. The sidecar GET is the one per-acquisition
        round trip in an otherwise single-LIST task walk, so we resolve the
        fetches through a small thread pool (:data:`_SIDECAR_WORKERS`) rather than
        one at a time -- but yield strictly in the input order, so ``search``
        output stays deterministic. Fetching in windows keeps the pool bounded
        and, because ``search`` is a generator that may stop early on ``limit`` /
        ``max_per_task``, caps wasted fetches at one window rather than an entire
        large task. A sidecar fetch that fails raises exactly as the serial path
        did (the pool re-raises when its result is consumed).
        """
        if not pending:
            return
        if len(pending) == 1:
            acq_prefix, keys, sidecar_url = pending[0]
            item = self._item_from_sidecar(self._get(sidecar_url), acq_prefix, keys, sidecar_url)
            if item is not None:
                yield item
            return

        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        def fetch(entry: tuple[str, list[str], str]) -> dict:
            return self._get(entry[2])

        workers = min(_SIDECAR_WORKERS, len(pending))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for base in range(0, len(pending), workers):
                window = pending[base : base + workers]
                for entry, doc in zip(window, pool.map(fetch, window), strict=True):
                    acq_prefix, keys, sidecar_url = entry
                    item = self._item_from_sidecar(doc, acq_prefix, keys, sidecar_url)
                    if item is not None:
                        yield item

    def _url_for(self, key: str) -> str:
        """Build a public HTTPS URL for an S3 key, encoding spaces / unicode.

        Named task directories like ``Allegiant Stadium`` and
        ``Atmospheric-River_Nov-2025`` show up under ``sar-data/tasks/``
        (UUID collects under ``sar-data/task-data/``) and contain
        characters that must be percent-encoded for CURL / rasterio to
        fetch them.
        """
        return f"{self._list_base}/{quote(key, safe='/')}"

    def _item_from_sidecar(
        self,
        doc: dict,
        acq_prefix: str,
        files: list[str],
        sidecar_url: str,
    ) -> UmbraItem | None:
        """Build an :class:`UmbraItem` from a v2 sidecar.

        The sidecars Umbra publishes reference asset URLs in a *private*
        bucket. The actual downloadable products sit next to the sidecar
        in the public bucket, so we discard the sidecar's asset hrefs and
        rebuild them from the keys we just listed -- the returned hrefs
        always resolve.
        """
        legacy = sidecar_url.endswith(_LEGACY_SIDECAR_SUFFIX)
        if legacy:
            stac = stac_from_legacy_metadata(doc)
            if stac is None:
                return None
            doc = stac
        assets: dict[str, dict[str, Any]] = {}
        for key in files:
            basename = key.rsplit("/", 1)[-1]
            if basename.endswith(_V2_SIDECAR_SUFFIX):
                continue
            assets[basename] = {
                "href": self._url_for(key),
                "type": _guess_media_type(basename),
            }
        if not assets:
            return None
        item = UmbraItem.from_dict({**doc, "assets": assets}, href=sidecar_url)
        # Record what the collect actually ships, so a caller (and the parquet)
        # can find a matched CPHD + SICD pair from one collect without
        # re-classifying asset keys. Namespaced to umbra-py: not Umbra's field.
        products = item.available_assets
        item.properties = {
            **item.properties,
            "umbra-py:products": products,
            "umbra-py:cphd_sicd_pair": "CPHD" in products and "SICD" in products,
            "umbra-py:layout": "legacy" if legacy else "v2",
        }
        item.raw = {**item.raw, "properties": item.properties}
        return item
