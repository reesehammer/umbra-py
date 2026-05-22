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
from collections.abc import Iterator
from datetime import date, datetime
from urllib.parse import urljoin

import requests

from ._http import default_session, get_json
from .constants import DEFAULT_STAC_ROOT
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


class UmbraCatalog:
    """Client for traversing and searching Umbra's open STAC catalog."""

    def __init__(
        self,
        root_url: str = DEFAULT_STAC_ROOT,
        session: requests.Session | None = None,
    ) -> None:
        self.root_url = root_url
        self.session = session or default_session()

    def _get(self, url: str) -> dict:
        try:
            return get_json(url, session=self.session)
        except requests.RequestException as exc:
            raise CatalogError(f"Failed to read catalog document {url!r}: {exc}") from exc

    @staticmethod
    def _links(doc: dict, rel: str) -> list[dict]:
        return [link for link in doc.get("links", []) if link.get("rel") == rel]

    def search(
        self,
        *,
        bbox: BBox | None = None,
        start: DateLike = None,
        end: DateLike = None,
        product_types: list[str] | None = None,
        limit: int | None = None,
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
        """
        start_d = _coerce_date(start)
        end_d = _coerce_date(end)
        wanted = {p.upper() for p in product_types} if product_types else None

        count = 0
        root = self._get(self.root_url)
        for item in self._walk(self.root_url, root, start_d, end_d):
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
