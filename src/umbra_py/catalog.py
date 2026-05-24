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

from ._http import default_session, get_json
from .constants import S3_BUCKET, S3_REGION
from .exceptions import CatalogError
from .models import BBox, UmbraItem

DateLike = str | date | datetime | None

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_TASKS_PREFIX = "sar-data/tasks/"
# Acquisition directories look like 2025-12-06-07-52-28_UMBRA-10/. Match the
# leading YYYY-MM-DD to prune subtrees before fetching their contents.
_ACQ_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-")
# tasks/<task>/[<uuid>/]<acquisition>/ — bounded recursion so a malformed
# bucket layout can never run away.
_MAX_WALK_DEPTH = 4

_GEOTIFF_MEDIA = "image/tiff; application=geotiff; profile=cloud-optimized"
_NITF_MEDIA = "application/vnd.nitf"
_JSON_MEDIA = "application/json"
_OCTET_MEDIA = "application/octet-stream"


def _coerce_date(value: DateLike) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


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
    """Client for searching Umbra's published open SAR data."""

    def __init__(
        self,
        bucket: str = S3_BUCKET,
        region: str = S3_REGION,
        session: requests.Session | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.session = session or default_session()
        self._list_base = f"https://s3.{region}.amazonaws.com/{bucket}"

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
            url = f"{self._list_base}/?prefix={quote(prefix)}&delimiter=/"
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

    # -- search ----------------------------------------------------------------

    def search(
        self,
        *,
        bbox: BBox | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[UmbraItem]:
        """Yield items matching the filters.

        Parameters
        ----------
        bbox:
            ``(min_lon, min_lat, max_lon, max_lat)`` footprint filter.
        start, end:
            Inclusive acquisition-date bounds. Accepts ``date`` /
            ``datetime`` objects or ISO ``YYYY-MM-DD`` strings. Strongly
            recommended -- without them the walker scans every published
            acquisition, which takes minutes.
        product_types:
            Keep only items exposing at least one of these assets
            (e.g. ``["GEC"]``).
        limit:
            Stop after yielding this many items.
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end)
        wanted = {p.upper() for p in product_types} if product_types else None

        count = 0
        for item in self._walk(_TASKS_PREFIX, depth=0, start=start_d, end=end_d):
            if bbox is not None and not item.intersects_bbox(bbox):
                continue
            if wanted is not None and not (wanted & set(item.available_assets)):
                continue
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    def _walk(
        self,
        prefix: str,
        depth: int,
        start: date | None,
        end: date | None,
    ) -> Iterator[UmbraItem]:
        subdirs, files = self._list_prefix(prefix)

        # An acquisition directory is the one that contains the sidecar.
        sidecar_key = next((k for k in files if k.endswith(".stac.v2.json")), None)
        if sidecar_key is not None:
            d = _acq_date(prefix)
            if d is not None:
                if start is not None and d < start:
                    return
                if end is not None and d > end:
                    return
            sidecar_url = f"{self._list_base}/{sidecar_key}"
            item = self._item_from_sidecar(self._get(sidecar_url), prefix, files, sidecar_url)
            if item is not None:
                yield item
            return

        if depth >= _MAX_WALK_DEPTH:
            return

        for sub in subdirs:
            sub_date = _acq_date(sub)
            if sub_date is not None:
                if start is not None and sub_date < start:
                    continue
                if end is not None and sub_date > end:
                    continue
            yield from self._walk(sub, depth + 1, start, end)

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
                "href": f"{self._list_base}/{key}",
                "type": _guess_media_type(basename),
            }
        if not assets:
            return None
        return UmbraItem.from_dict({**doc, "assets": assets}, href=sidecar_url)
