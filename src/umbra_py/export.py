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

from .constants import (
    ASSET_SCHEMA_FIELD,
    ASSET_SCHEMA_VERSION,
    STAC_BUILT_AT_FIELD,
    STAC_SNAPSHOT_FIELD,
)
from .exceptions import MissingDependencyError, UmbraError
from .models import UmbraItem, _iso_utc, _normalize_assets

#: The collection id stamped into geoparquet file metadata. Same value as
#: ``umbra_py.serve.COLLECTION_ID``; kept here so export does not import the
#: server. A test asserts the two stay equal.
PARQUET_COLLECTION_ID = "umbra-open-data"


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
    # ``created`` is already in the STAC properties when the sidecar had it.
    # The baked place label and ``published`` (bucket LastModified) are index
    # columns, injected so a parquet consumer sees them without re-reading
    # SQLite. Rebind properties to a fresh dict so ``item.raw`` is not mutated.
    # An existing ``umbra:place`` / ``umbra-py:published`` on the source wins.
    # Asset keys are schema v2 even when ``item.raw`` still has filenames.
    props = dict(doc.get("properties") or {})
    if item.place and "umbra:place" not in props:
        props["umbra:place"] = item.place
    published = _iso_utc(item.published)
    if published and "umbra-py:published" not in props:
        props["umbra-py:published"] = published
    props[ASSET_SCHEMA_FIELD] = ASSET_SCHEMA_VERSION
    doc["properties"] = props
    doc["assets"] = _normalize_assets(doc.get("assets") or item.assets)
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


def _parquet_collection(built_at: str | None, snapshot: str | None) -> dict[str, Any]:
    """Collection document stored in the parquet file metadata.

    File-level only: it is not a column, and it does not touch ``assets``.
    ``updated`` is the same midnight-UTC rendering as the STAC collection.
    """
    from .serve import _collection_updated  # noqa: PLC0415

    doc: dict[str, Any] = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": PARQUET_COLLECTION_ID,
        # Same stamp as the served collection and each exported item.
        ASSET_SCHEMA_FIELD: ASSET_SCHEMA_VERSION,
    }
    if built_at:
        doc[STAC_BUILT_AT_FIELD] = built_at
        doc["updated"] = _collection_updated(built_at)
    if snapshot:
        doc[STAC_SNAPSHOT_FIELD] = snapshot
    return doc


def _write_geoparquet(
    reader: Any,
    path: Path,
    *,
    built_at: str | None,
    snapshot: str | None,
) -> None:
    """Write ``reader`` and stamp build provenance into the file metadata.

    Uses stac-geoparquet's metadata builder so the ``geo`` and
    ``stac-geoparquet`` keys stay intact, then adds ``umbra-py:built_at`` and
    ``umbra-py:snapshot`` as sibling key/value entries. Row groups, including
    the ``assets`` column, are whatever ``parse_stac_items_to_arrow`` produced.
    """
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415
    import stac_geoparquet.arrow as sga  # noqa: PLC0415
    from stac_geoparquet.arrow._to_parquet import create_parquet_metadata  # noqa: PLC0415

    stream = pa.RecordBatchReader.from_stream(reader)
    meta = create_parquet_metadata(
        stream.schema,
        schema_version=sga.DEFAULT_PARQUET_SCHEMA_VERSION,
        collections={PARQUET_COLLECTION_ID: _parquet_collection(built_at, snapshot)},
    )
    if built_at:
        meta[STAC_BUILT_AT_FIELD.encode("utf-8")] = built_at.encode("utf-8")
    if snapshot:
        meta[STAC_SNAPSHOT_FIELD.encode("utf-8")] = snapshot.encode("utf-8")
    schema = stream.schema.with_metadata(meta)
    with pq.ParquetWriter(str(path), schema) as writer:
        for batch in stream:
            writer.write_batch(batch)


def export_geoparquet(
    items: Iterable[UmbraItem],
    path: str | os.PathLike,
    *,
    built_at: str | None = None,
    snapshot: str | None = None,
) -> int:
    """Write items to a stac-geoparquet file; return how many were written.

    Items without a footprint geometry are skipped — the geometry column is
    the point of geoparquet, and the writer requires one — so the return
    value can be less than the number of items passed in. Raises
    :class:`~umbra_py.UmbraError` when nothing is exportable, rather than
    writing an empty (and schema-less) file.

    ``built_at`` and ``snapshot`` are written as file-level key/value metadata
    (and inside the ``stac-geoparquet`` collection document, next to
    ``umbra-py:asset_schema``). They are not columns. Omit both and the
    collection metadata still records the asset schema.
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
    # File metadata, next to the per-item property. The collection id matches
    # the one ``umbra serve`` publishes. Asset schema is always recorded;
    # build date and snapshot are added when this export has them.
    if built_at or snapshot:
        _write_geoparquet(reader, path, built_at=built_at, snapshot=snapshot)
    else:
        stac_geoparquet.arrow.to_parquet(
            reader,
            path,
            collections={PARQUET_COLLECTION_ID: _parquet_collection(None, None)},
        )
    return len(docs)
