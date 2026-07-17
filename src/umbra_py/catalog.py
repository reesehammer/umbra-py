"""Search Umbra's published open SAR data.

Umbra publishes each acquisition under
``s3://umbra-open-data-catalog/sar-data/tasks/<task>/[<uuid>/]<acquisition>/``,
with a ``*.stac.v2.json`` sidecar next to the binary products. The legacy
``stac/`` tree of ``catalog.json`` files lists thousands of items, but most
reference data that was never actually published — searching it returns
items whose download URLs don't resolve.

:class:`UmbraCatalog` walks the live ``sar-data/tasks/`` prefix directly
via paginated S3 listings. Acquisition directory names start with the
acquisition date (``YYYY-MM-DD-HH-MM-SS_PLATFORM``), so a search bounded by
``start`` / ``end`` prunes whole subtrees without fetching them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

import requests

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
# Acquisition directories look like 2025-12-06-07-52-28_UMBRA-10/. We use the
# leading YYYY-MM-DD both to identify the acquisition component of a key and
# to prune by date.
_ACQ_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")

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
    """Task directory name (the AOI label) from a ``sar-data/tasks/<name>/``
    prefix, e.g. ``"Centerfield, Utah"``. S3 keys are unencoded, so the name
    carries its literal spaces / commas."""
    return task_prefix[len(_TASKS_PREFIX) :].rstrip("/")


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
            root = ET.fromstring(resp.content)
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
            root = ET.fromstring(resp.content)
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
            range, so even a narrow window takes a few seconds; provide
            ``limit`` to stop as soon as you have enough.
        product_types:
            Keep only items exposing at least one of these assets
            (e.g. ``["GEC"]``).
        area:
            Case-insensitive substring matched against each
            ``sar-data/tasks/<task>/`` directory name. Umbra files every
            pass of a site under one named task directory (e.g.
            ``"Centerfield, Utah"``), so ``area="centerfield"`` returns
            just that site's acquisitions. Non-matching task directories
            are skipped *before* they're listed, so this also makes the
            search much faster -- the ergonomic way to gather the
            co-located passes a change composite needs.
        fuzzy:
            Widen ``area`` from a literal substring to a deterministic
            token-wise fuzzy match (:func:`umbra_py.fuzzy.task_matches`):
            word-order- and punctuation-independent, tolerant of a small
            typo, and a strict superset of the substring match (it never
            drops a result). So ``area="utah centerfield"`` or
            ``area="centrfield"`` still reaches ``"Centerfield, Utah"``.
            No model call -- see ``docs/AI_INTEGRATION_IDEAS.md`` C1.
        limit:
            Stop after yielding this many items.
        max_per_task:
            Cap the number of items yielded from any one
            ``sar-data/tasks/<task>/`` directory. Each task is a tasking
            campaign over the same area, so ``max_per_task=1`` swaps the
            usual "every revisit of a few sites" output for "one
            acquisition per distinct site" -- much better diversity on a
            map.

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

        if self.token:
            yield from self._search_archive(
                bbox=bbox,
                intersects=intersects,
                start=start_d,
                end=end_d,
                wanted=wanted,
                area=area,
                fuzzy=fuzzy,
                limit=limit,
                max_per_task=max_per_task,
            )
            return

        task_subdirs, _ = self._list_prefix(_TASKS_PREFIX)
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
        limit: int | None,
        max_per_task: int | None,
    ) -> Iterator[UmbraItem]:
        """Search the Canopy commercial archive over its STAC API.

        Umbra's commercial product *does* expose a real STAC API, so unlike the
        open bucket we POST a standard STAC item-search body and follow the
        ``rel="next"`` pagination links, building an :class:`UmbraItem` from each
        returned feature (whose asset hrefs are already resolvable URLs, so no
        rewrite is needed). ``bbox`` and the date interval are pushed down to the
        API; ``product_types`` and ``area``/``fuzzy`` are applied client-side to
        keep exact parity with the open-bucket walk.
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
            rel = key[len(task_prefix) :]
            parts = rel.split("/")
            # The acquisition component is the first segment matching the
            # date pattern; skip anything without one (stray bucket junk).
            acq_idx = next(
                (i for i, p in enumerate(parts[:-1]) if _ACQ_DATE_RE.match(p)),
                None,
            )
            if acq_idx is None:
                continue
            d = _acq_date(parts[acq_idx])
            if start is not None and d is not None and d < start:
                continue
            if end is not None and d is not None and d > end:
                continue
            acq_prefix = task_prefix + "/".join(parts[: acq_idx + 1]) + "/"
            by_acq.setdefault(acq_prefix, []).append(key)

        # Collect the acquisitions that have a sidecar, sorted so output order is
        # deterministic (older acquisitions first). Each still needs one sidecar
        # GET -- the N+1 round trips in an otherwise single-LIST walk -- which
        # _items_from_sidecars resolves concurrently while preserving this order.
        pending: list[tuple[str, list[str], str]] = []
        for acq_prefix in sorted(by_acq):
            keys = by_acq[acq_prefix]
            sidecar = next((k for k in keys if k.endswith(".stac.v2.json")), None)
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
        and contain characters that must be percent-encoded for CURL /
        rasterio to fetch them.
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
        assets: dict[str, dict[str, Any]] = {}
        for key in files:
            basename = key.rsplit("/", 1)[-1]
            if basename.endswith(".stac.v2.json"):
                continue
            assets[basename] = {
                "href": self._url_for(key),
                "type": _guess_media_type(basename),
            }
        if not assets:
            return None
        return UmbraItem.from_dict({**doc, "assets": assets}, href=sidecar_url)
