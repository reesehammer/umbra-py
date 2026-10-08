"""Coverage parity: how much of the open-data bucket a catalog index covers.

The index is only worth citing if it is complete, so this module measures that
directly instead of trusting the walker: list every object under
``sar-data/`` and ``open-data/`` (paginated, non-delimited listings), group the keys into
acquisitions with the *same* grouping the walker uses
(:func:`umbra_py.catalog._acquisition_group`), and compare against the sidecar
hrefs in a built :class:`~umbra_py.CatalogIndex`. The report breaks the
result down by prefix (``tasks/`` vs ``task-data/``), by sidecar layout (v2
STAC, legacy ``_METADATA.json``, none) and by product (GEC, CSI, SIDD, SICD,
CPHD), and lists what is missing so a gap is actionable rather than a number.

An acquisition with no sidecar of either kind has no footprint or time to index
and is reported as ``unindexable`` rather than missing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from .catalog import (
    _DATA_PREFIXES,
    _LEGACY_SIDECAR_SUFFIX,
    _V2_SIDECAR_SUFFIX,
    UmbraCatalog,
    _acquisition_group,
)
from .constants import PRODUCT_ASSETS, S3_BUCKET
from .models import _classify_asset

#: The bucket roots published acquisitions live under.
DATA_ROOTS = ("sar-data/", "open-data/")

#: How many missing acquisitions the report lists by name (the counts are exact).
MISSING_LIST_LIMIT = 500


@dataclass
class Acquisition:
    """One acquisition in the bucket: its group id, layout and products."""

    prefix: str
    task: str
    group: str
    layout: str
    products: set[str] = field(default_factory=set)


def _split(key: str) -> tuple[str, str, str] | None:
    """``(data_prefix, task, rel)`` for a key under one of the data prefixes."""
    for data_prefix in _DATA_PREFIXES:
        if key.startswith(data_prefix):
            rest = key[len(data_prefix) :]
            task, sep, rel = rest.partition("/")
            if not sep or not task:
                return None
            return data_prefix, task, rel
    return None


def group_key(key: str) -> str | None:
    """The absolute acquisition group id of a bucket key, or ``None``."""
    split = _split(key)
    if split is None:
        return None
    data_prefix, task, rel = split
    group = _acquisition_group(rel)
    if group is None:
        return None
    return f"{data_prefix}{task}/{group[0]}"


def bucket_acquisitions(keys: Iterable[str]) -> dict[str, Acquisition]:
    """Group raw bucket keys into acquisitions keyed by :func:`group_key`."""
    acqs: dict[str, Acquisition] = {}
    sidecars: dict[str, set[str]] = {}
    for key in keys:
        split = _split(key)
        if split is None:
            continue
        data_prefix, task, rel = split
        group = _acquisition_group(rel)
        if group is None:
            continue
        gid = f"{data_prefix}{task}/{group[0]}"
        acq = acqs.get(gid)
        if acq is None:
            acq = acqs[gid] = Acquisition(prefix=data_prefix, task=task, group=gid, layout="none")
        basename = key.rsplit("/", 1)[-1]
        if basename.endswith(_V2_SIDECAR_SUFFIX):
            sidecars.setdefault(gid, set()).add("v2")
            continue
        if basename.endswith(_LEGACY_SIDECAR_SUFFIX):
            sidecars.setdefault(gid, set()).add("legacy")
            continue
        product = _classify_asset(basename, {})
        if product in PRODUCT_ASSETS:
            acq.products.add(product)
    for gid, kinds in sidecars.items():
        acqs[gid].layout = "v2" if "v2" in kinds else "legacy"
    return acqs


def list_bucket_keys(catalog: UmbraCatalog | None = None) -> list[str]:
    """Every object key under the data roots (about 100 paginated LIST calls)."""
    catalog = catalog or UmbraCatalog()
    return [key for root in DATA_ROOTS for key in catalog._stream_keys(root)]


def _href_to_key(href: str) -> str | None:
    marker = f"/{S3_BUCKET}/"
    idx = href.find(marker)
    if idx == -1:
        return None
    return unquote(href[idx + len(marker) :])


def indexed_groups(index: Any) -> dict[str, set[str]]:
    """Map each indexed acquisition group to the products its item exposes."""
    conn = index._conn
    products: dict[str, set[str]] = {}
    for href, asset in conn.execute(
        "SELECT i.href, a.asset FROM items i LEFT JOIN item_assets a ON a.href = i.href"
    ):
        key = _href_to_key(href)
        gid = group_key(key) if key else None
        if gid is None:
            continue
        bucket = products.setdefault(gid, set())
        if asset:
            bucket.add(asset)
    return products


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 2) if den else 100.0


def coverage_report(
    index: Any,
    keys: Iterable[str],
    *,
    missing_limit: int = MISSING_LIST_LIMIT,
) -> dict[str, Any]:
    """Compare a built index against a bucket listing.

    ``index`` is an open :class:`~umbra_py.CatalogIndex`; ``keys`` is every
    object key under the data roots (see :func:`list_bucket_keys`). Coverage is
    counted in acquisitions. ``pct`` is indexed over every acquisition in the
    bucket; ``pct_indexable`` excludes acquisitions with no sidecar at all.
    """
    acqs = bucket_acquisitions(keys)
    indexed = indexed_groups(index)
    rows, distinct = index._conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id) FROM items"
    ).fetchone()

    by_prefix: dict[str, dict[str, int]] = {}
    by_layout: dict[str, dict[str, int]] = {}
    by_product: dict[str, dict[str, dict[str, int]]] = {}
    missing: list[dict[str, Any]] = []
    unindexable: list[str] = []
    for gid in sorted(acqs):
        acq = acqs[gid]
        hit = gid in indexed
        for table, name in ((by_prefix, acq.prefix), (by_layout, acq.layout)):
            row = table.setdefault(name, {"bucket": 0, "indexed": 0})
            row["bucket"] += 1
            row["indexed"] += int(hit)
        per = by_product.setdefault(acq.prefix, {})
        for product in acq.products:
            row = per.setdefault(product, {"bucket": 0, "indexed": 0})
            row["bucket"] += 1
            row["indexed"] += int(hit and product in indexed[gid])
        if not hit:
            if acq.layout == "none":
                unindexable.append(gid)
            else:
                missing.append(
                    {"group": gid, "layout": acq.layout, "products": sorted(acq.products)}
                )

    def finish(row: dict[str, int]) -> dict[str, Any]:
        return {**row, "pct": _pct(row["indexed"], row["bucket"])}

    total = len(acqs)
    hit_total = sum(1 for gid in acqs if gid in indexed)
    indexable = total - len(unindexable)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index": {
            "path": str(getattr(index, "path", "")),
            "built_at": index.get_meta("built_at"),
            "rows": rows,
            "distinct_ids": distinct,
        },
        "totals": {
            "bucket": total,
            "indexable": indexable,
            "indexed": hit_total,
            "pct": _pct(hit_total, total),
            "pct_indexable": _pct(hit_total, indexable),
        },
        "by_prefix": {k: finish(v) for k, v in sorted(by_prefix.items())},
        "by_layout": {k: finish(v) for k, v in sorted(by_layout.items())},
        "by_product": {
            prefix: {p: finish(per[p]) for p in PRODUCT_ASSETS if p in per}
            for prefix, per in sorted(by_product.items())
        },
        "missing_count": len(missing),
        "missing": missing[:missing_limit],
        "unindexable": unindexable,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """A short human-readable rendering of :func:`coverage_report`."""
    t = report["totals"]
    idx = report["index"]
    lines = [
        "# umbra-py index coverage",
        "",
        f"Generated {report['generated_at']} against index built {idx['built_at']} "
        f"({idx['rows']} rows, {idx['distinct_ids']} distinct ids).",
        "",
        f"**Overall: {t['indexed']} of {t['bucket']} bucket acquisitions indexed "
        f"({t['pct']}%; {t['pct_indexable']}% of the {t['indexable']} with a sidecar).**",
        "",
        "| Prefix | Bucket | Indexed | % |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, row in report["by_prefix"].items():
        lines.append(f"| {name} | {row['bucket']} | {row['indexed']} | {row['pct']} |")
    lines += ["", "| Sidecar layout | Bucket | Indexed | % |", "| --- | ---: | ---: | ---: |"]
    for name, row in report["by_layout"].items():
        lines.append(f"| {name} | {row['bucket']} | {row['indexed']} | {row['pct']} |")
    lines += [
        "",
        "| Prefix | Product | Bucket | Indexed | % |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for prefix, per in report["by_product"].items():
        for product, row in per.items():
            lines.append(
                f"| {prefix} | {product} | {row['bucket']} | {row['indexed']} | {row['pct']} |"
            )
    lines += [
        "",
        f"Missing: {report['missing_count']}. Unindexable (no sidecar): "
        f"{len(report['unindexable'])}.",
        "",
        "Contains Umbra open data, licensed under CC BY 4.0.",
        "",
    ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], json_path: str | None, md_path: str | None) -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    if md_path:
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
