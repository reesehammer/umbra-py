"""Find Umbra acquisitions with a hand-rolled, date-pruned traversal.

Why this is here
----------------
``pystac`` walks the whole static catalog for every query (see
01_search_catalog_pystac.py). To make a "one day in 2024" search fast, you
have to walk the tree yourself and *prune* child catalogs whose date span
cannot overlap the query before you fetch them.

The trick is that Umbra encodes the date span of each child catalog in its
directory token::

    stac/catalog.json
      -> stac/2024/catalog.json                       # token: 2024
        -> stac/2024/2024-02/catalog.json             # token: 2024-02
          -> stac/2024/2024-02/2024-02-08/catalog.json
                                                      # token: 2024-02-08
            -> <item>.json files

A token of ``2024-02`` covers all of February 2024. So if your query is
"2024-02-08 only", you can skip ``2024-01``, ``2024-03``, ..., the entirety
of 2023, etc. without fetching their catalogs.

That heuristic is what umbra-py's ``UmbraCatalog.search`` does internally.

Requires::

    pip install requests

Run::

    python 02_search_catalog_handrolled.py
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import urljoin

import requests

STAC_ROOT = (
    "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/stac/catalog.json"
)

_TOKEN_RE = re.compile(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")


def token_span(token: str) -> tuple[date, date] | None:
    """Inclusive date span covered by a catalog directory token.

    ``"2024"`` -> Jan 1 .. Dec 31.  ``"2024-02"`` -> Feb 1 .. Feb 29.
    ``"2024-02-08"`` -> a single day. Returns ``None`` if not a date token.
    """
    m = _TOKEN_RE.search(token)
    if not m:
        return None
    y_s, mo_s, d_s = m.groups()
    y = int(y_s)
    if d_s is not None:
        d = date(y, int(mo_s), int(d_s))
        return d, d
    if mo_s is not None:
        m_i = int(mo_s)
        last = calendar.monthrange(y, m_i)[1]
        return date(y, m_i, 1), date(y, m_i, last)
    return date(y, 1, 1), date(y, 12, 31)


def spans_overlap(
    span: tuple[date, date], start: date | None, end: date | None
) -> bool:
    lo, hi = span
    if start is not None and hi < start:
        return False
    if end is not None and lo > end:
        return False
    return True


def token_from_href(href: str) -> str:
    parts = [p for p in href.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if parts and parts[-1].endswith(".json"):
        parts = parts[:-1]
    return parts[-1] if parts else ""


def bbox_from_geometry(geometry: dict) -> tuple[float, float, float, float] | None:
    """Extract a 2D bounding box from a (possibly 3D) STAC geometry.

    Umbra's items ship a 3D polygon (lon, lat, height). The standard
    ``shapely.geometry.shape(...).bounds`` works, but pulling in shapely just
    to compute a bbox is overkill for a small example.
    """
    coords = geometry.get("coordinates") if geometry else None
    if not coords:
        return None
    lons: list[float] = []
    lats: list[float] = []

    def walk(node):
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not lons:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def overlaps(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def walk(
    url: str,
    start: date | None,
    end: date | None,
    bbox: tuple[float, float, float, float] | None,
    session: requests.Session,
) -> Iterator[dict]:
    doc = session.get(url, timeout=30).json()

    for link in doc.get("links", []):
        rel = link.get("rel")
        href = link.get("href")
        if not href:
            continue

        if rel == "item":
            item_url = urljoin(url, href)
            item = session.get(item_url, timeout=30).json()
            if bbox is not None:
                item_bbox = item.get("bbox") or bbox_from_geometry(
                    item.get("geometry") or {}
                )
                if item_bbox is None or not overlaps(item_bbox[:4], bbox):
                    continue
            yield item

        elif rel == "child":
            token = token_from_href(link.get("title") or href)
            span = token_span(token)
            if span is not None and not spans_overlap(span, start, end):
                continue
            yield from walk(urljoin(url, href), start, end, bbox, session)


def main() -> None:
    start = date(2024, 2, 8)
    end = date(2024, 2, 8)
    bbox = None  # e.g. (-68.1, 10.4, -67.9, 10.6)

    session = requests.Session()
    found = 0
    for item in walk(STAC_ROOT, start, end, bbox, session):
        print(item["id"], "->", list(item.get("assets", {})))
        found += 1
        if found >= 5:
            break


if __name__ == "__main__":
    main()
