"""Export catalog items to stac-geoparquet (optional, requires the ``export`` extra).

Umbra publishes no STAC API, so answering "what's in the catalog?" means
walking the bucket live (``catalog.py``) or querying a local index
(``index.py``) — and either way, every user pays for their own crawl.
`stac-geoparquet <https://stac-geoparquet.org/>`__ is the cloud-native fix:
one Parquet file holding every STAC item, searchable in seconds by DuckDB,
geopandas, pyarrow or rustac with no server and no crawl. Exporting an index
turns the one-time walk into a shareable artifact — the pipeline behind the
published catalog snapshot (see ``.github/workflows/publish-index.yml``).

Install with: ``pip install "umbra-py[export]"``
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .exceptions import MissingDependencyError, UmbraError
from .models import UmbraItem


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise MissingDependencyError(
            f"'{module}' is required for geoparquet export. "
            'Install the extra with: pip install "umbra-py[export]"',
            hint='pip install "umbra-py[export]"',
        ) from exc


def _export_doc(item: UmbraItem) -> dict[str, Any]:
    """The item's raw STAC dict, with a ``self`` link back to its sidecar.

    The sidecar URL is how a parquet consumer gets from a row back to the
    catalog (and from there to the data files), but Umbra's published items
    don't carry it — it is only known from where the walk found the JSON.
    Inject it as the standard ``self`` link, without mutating ``item.raw``.
    """
    doc = dict(item.raw)
    links = list(doc.get("links") or [])
    if item.href and not any(link.get("rel") == "self" for link in links):
        links.append({"rel": "self", "href": item.href, "type": "application/json"})
    doc["links"] = links
    # Carry the baked reverse-geocoded label (from `umbra index bake`) into the
    # published snapshot as a namespaced property, so a parquet consumer gets a
    # real place name without re-geocoding every row. Only when the index
    # resolved one and the raw item didn't already carry it.
    if item.place:
        props = dict(doc.get("properties") or {})
        props.setdefault("umbra:place", item.place)
        doc["properties"] = props
    return doc


def _normalize_mixed_property_types(docs: list[dict[str, Any]]) -> None:
    """Make properties that drift between list and scalar uniform across ``docs``.

    Umbra's catalog is not perfectly uniform: some acquisitions encode a
    property as a single object where others -- and the STAC spec -- use a
    list. ``providers`` is the observed case: a list of provider objects on
    most items, but a bare object on a handful. stac-geoparquet infers one
    Arrow type per column, so a column that is a list in one row and a scalar
    in another aborts the whole export with ``cannot mix list and non-list,
    non-null values``. Wrap the scalar occurrences in a single-element list so
    the column is uniform; this is lossless and, for ``providers``, is the
    spec-correct shape. Rebinds each doc's ``properties`` to a fresh dict, so
    ``item.raw`` is never mutated.
    """
    kinds: dict[str, set[str]] = {}
    for doc in docs:
        for key, val in (doc.get("properties") or {}).items():
            if val is None:
                continue
            kinds.setdefault(key, set()).add("list" if isinstance(val, list) else "scalar")
    mixed = {key for key, seen in kinds.items() if seen == {"list", "scalar"}}
    if not mixed:
        return
    for doc in docs:
        props = doc.get("properties")
        if not props:
            continue
        normalized = dict(props)
        for key in mixed:
            val = normalized.get(key)
            if val is not None and not isinstance(val, list):
                normalized[key] = [val]
        doc["properties"] = normalized


def export_geoparquet(items: Iterable[UmbraItem], path: str | os.PathLike) -> int:
    """Write items to a stac-geoparquet file; return how many were written.

    Items without a footprint geometry are skipped — the geometry column is
    the point of geoparquet, and the writer requires one — so the return
    value can be less than the number of items passed in. Raises
    :class:`~umbra_py.UmbraError` when nothing is exportable, rather than
    writing an empty (and schema-less) file.
    """
    _require("stac_geoparquet")
    import stac_geoparquet.arrow  # noqa: PLC0415

    docs = [_export_doc(item) for item in items if item.geometry]
    if not docs:
        raise UmbraError("No items with a footprint geometry to export.")
    _normalize_mixed_property_types(docs)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reader = stac_geoparquet.arrow.parse_stac_items_to_arrow(docs)
    stac_geoparquet.arrow.to_parquet(reader, path)
    return len(docs)
