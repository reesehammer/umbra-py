"""Find Umbra acquisitions using only `pystac` (no umbra-py).

Why this is here
----------------
Umbra publishes its open data through a *static* STAC catalog: a tree of
``catalog.json`` files in the public S3 bucket ``umbra-open-data-catalog``,
rooted at::

    https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/stac/catalog.json

There is no STAC API endpoint, so ``pystac-client.Client.open(...)`` is not
applicable. The next obvious tool is ``pystac.Catalog`` which understands the
static catalog format and will happily walk it for you.

The catch:

- The catalog partitions items by date (``year/year-month/year-month-day``).
- ``Catalog.get_items(recursive=True)`` walks *every* child catalog, regardless
  of whether its date span overlaps your query.
- For Umbra that means several hundred HTTP requests just to find the items
  that live under, say, "2024-02-08" — even if you only care about that day.

So this works, but it is slow against a 17 TB bucket index. For anything
narrower than "everything since the bucket existed" you end up writing a
date-aware traversal by hand (see 02_search_catalog_handrolled.py).

Requires::

    pip install pystac requests

Run::

    python 01_search_catalog_pystac.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pystac

STAC_ROOT = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/stac/catalog.json"


def search_pystac(
    start: datetime,
    end: datetime,
    bbox: tuple[float, float, float, float] | None = None,
    limit: int = 5,
) -> list[pystac.Item]:
    """Walk the static catalog and post-filter items by date and bbox.

    Note that ``get_items(recursive=True)`` reads every child catalog. There
    is no way to push date or bbox filters down to the traversal — the
    pruning has to happen *after* each leaf is fetched.
    """
    root = pystac.Catalog.from_file(STAC_ROOT)
    matches: list[pystac.Item] = []

    for item in root.get_items(recursive=True):
        item_dt = item.datetime
        if item_dt is None:
            props = item.properties or {}
            stamp = props.get("start_datetime") or props.get("datetime")
            if stamp is None:
                continue
            item_dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if item_dt.tzinfo is None:
            item_dt = item_dt.replace(tzinfo=timezone.utc)

        if item_dt < start or item_dt > end:
            continue

        if bbox is not None and item.bbox is not None:
            ib = item.bbox
            if ib[2] < bbox[0] or ib[0] > bbox[2] or ib[3] < bbox[1] or ib[1] > bbox[3]:
                continue

        matches.append(item)
        if len(matches) >= limit:
            break

    return matches


def main() -> None:
    start = datetime(2024, 2, 8, tzinfo=timezone.utc)
    end = datetime(2024, 2, 8, 23, 59, 59, tzinfo=timezone.utc)

    print(f"searching pystac tree {start.date()} -> {end.date()} ...")
    print("(this will fetch many catalog.json files; expect tens of seconds)")
    items = search_pystac(start, end, limit=5)
    for item in items:
        print(item.id, "->", list(item.assets))


if __name__ == "__main__":
    main()
