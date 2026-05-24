"""Search Umbra's static STAC catalog.

The catalog is a tree of ``catalog.json`` files partitioned by date
(``year`` -> ``year-month`` -> ``year-month-day`` -> items). Because it is a
static catalog with no search API, :class:`UmbraCatalog` walks the tree but
*prunes* whole branches whose date range cannot match the query, so a search
constrained by date only fetches the relevant day catalogs.
"""

from __future__ import annotations

import calendar
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date, datetime
from urllib.parse import quote, urljoin

import requests

from ._http import default_session, get_json
from .constants import DEFAULT_STAC_ROOT, S3_BUCKET, S3_REGION
from .exceptions import CatalogError
from .models import BBox, UmbraItem

DateLike = str | date | datetime | None

# Catalog directory tokens look like 2024, 2024-01 or 2024-01-01.
_TOKEN_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def _coerce_date(value: DateLike) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _token_span(token: str) -> tuple[date, date] | None:
    """Map a catalog token (``2024`` / ``2024-01`` / ``2024-01-01``) to the
    inclusive date span it covers, or ``None`` if it is not a date token."""
    match = _TOKEN_RE.search(token)
    if not match:
        return None
    year, month, day = match.groups()
    y = int(year)
    if day is not None:
        d = date(y, int(month), int(day))
        return d, d
    if month is not None:
        m = int(month)
        last = calendar.monthrange(y, m)[1]
        return date(y, m, 1), date(y, m, last)
    return date(y, 1, 1), date(y, 12, 31)


def _spans_overlap(span: tuple[date, date], start: date | None, end: date | None) -> bool:
    lo, hi = span
    if start is not None and hi < start:
        return False
    if end is not None and lo > end:
        return False
    return True


def _token_from_href(href: str) -> str:
    """Extract the date token from a child catalog href like
    ``./2024-01/catalog.json`` -> ``2024-01``."""
    parts = [p for p in href.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    # Drop a trailing filename such as catalog.json.
    if parts and parts[-1].endswith(".json"):
        parts = parts[:-1]
    return parts[-1] if parts else ""


_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class UmbraCatalog:
    """Client for traversing and searching Umbra's open STAC catalog."""

    def __init__(
        self,
        root_url: str = DEFAULT_STAC_ROOT,
        session: requests.Session | None = None,
    ) -> None:
        self.root_url = root_url
        self.session = session or default_session()
        self._available_task_ids: set[str] | None = None

    def _get(self, url: str) -> dict:
        try:
            return get_json(url, session=self.session)
        except requests.RequestException as exc:
            raise CatalogError(f"Failed to read catalog document {url!r}: {exc}") from exc

    @staticmethod
    def _links(doc: dict, rel: str) -> list[dict]:
        return [link for link in doc.get("links", []) if link.get("rel") == rel]

    def available_task_ids(self) -> set[str]:
        """Return the task UUIDs that have downloadable data in the open bucket.

        Lists ``sar-data/tasks/?delimiter=/`` once (paginated) and keeps the
        UUID-style directory names. Used by ``search(data_available_only=True)``
        to prune items whose binary data was never published.

        Cached on the catalog instance after the first call.

        **Caveat:** the bucket also contains *named* task directories
        (``AIR/``, ``Atmospheric-River_Nov-2025/``, ...) holding most of the
        published imagery under sub-UUIDs. Those items have STAC v2 sidecars
        on disk but don't appear in the v1 STAC tree this catalog walks, so
        this method intentionally surfaces only the top-level UUID tasks
        ``search`` can actually return. See TODO.md for the larger pivot.
        """
        if self._available_task_ids is None:
            self._available_task_ids = self._fetch_available_task_ids()
        return self._available_task_ids

    def _fetch_available_task_ids(self) -> set[str]:
        uuids: set[str] = set()
        list_url = (
            f"https://s3.{S3_REGION}.amazonaws.com/{S3_BUCKET}/?prefix=sar-data/tasks/&delimiter=/"
        )
        token: str | None = None
        while True:
            url = list_url + (f"&continuation-token={quote(token)}" if token else "")
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                raise CatalogError(f"Failed to list bucket prefix {url!r}: {exc}") from exc
            root = ET.fromstring(resp.content)
            for cp in root.findall(f"{_S3_NS}CommonPrefixes"):
                prefix = cp.findtext(f"{_S3_NS}Prefix") or ""
                name = prefix.rstrip("/").rsplit("/", 1)[-1]
                if _UUID_RE.match(name):
                    uuids.add(name)
            if root.findtext(f"{_S3_NS}IsTruncated") != "true":
                break
            token = root.findtext(f"{_S3_NS}NextContinuationToken")
            if not token:
                break
        return uuids

    def search(
        self,
        *,
        bbox: BBox | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        limit: int | None = None,
        data_available_only: bool = False,
    ) -> Iterator[UmbraItem]:
        """Yield items matching the given filters.

        Parameters
        ----------
        bbox:
            ``(min_lon, min_lat, max_lon, max_lat)`` footprint filter.
        start, end:
            Inclusive acquisition-date bounds. Accepts ``date``/``datetime``
            objects or ISO ``YYYY-MM-DD`` strings.
        product_types:
            Keep only items exposing at least one of these assets
            (e.g. ``["GEC"]``).
        limit:
            Stop after yielding this many items.
        data_available_only:
            If true, only yield items whose ``umbra:task_id`` corresponds to
            a real directory in the public S3 bucket -- i.e. items whose
            asset URLs are guaranteed to resolve. Almost every item in
            Umbra's v1 STAC catalog has empty hrefs and references task IDs
            whose data was never made public; this filter prunes them out
            with a single bucket listing. Costs one extra HTTP request on
            the first call (results are cached on the catalog instance).
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end)
        wanted = {p.upper() for p in product_types} if product_types else None
        available = self.available_task_ids() if data_available_only else None

        count = 0
        root = self._get(self.root_url)
        for item in self._walk(self.root_url, root, start_d, end_d):
            if bbox is not None and not item.intersects_bbox(bbox):
                continue
            if wanted is not None and not (wanted & set(item.available_assets)):
                continue
            if available is not None:
                tid = item.properties.get("umbra:task_id")
                if not tid or tid not in available:
                    continue
            yield item
            count += 1
            if limit is not None and count >= limit:
                return

    def _walk(
        self,
        base_url: str,
        doc: dict,
        start: date | None,
        end: date | None,
    ) -> Iterator[UmbraItem]:
        # Yield any items attached directly to this catalog (leaf/day level).
        for link in self._links(doc, "item"):
            href = link.get("href")
            if not href:
                continue
            item_url = urljoin(base_url, href)
            yield UmbraItem.from_dict(self._get(item_url), href=item_url)

        # Descend into child catalogs, pruning those outside the date range.
        for link in self._links(doc, "child"):
            href = link.get("href")
            if not href:
                continue
            token = _token_from_href(link.get("title") or href)
            span = _token_span(token)
            if span is not None and not _spans_overlap(span, start, end):
                continue
            child_url = urljoin(base_url, href)
            yield from self._walk(child_url, self._get(child_url), start, end)
