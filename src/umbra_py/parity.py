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
and is reported as ``unindexable`` rather than missing. An acquisition the
index build dropped on purpose — a duplicate id, or a quarantined row — is
``accounted`` rather than missing, so the coverage gate does not fail on a
gap the validation report already accepted.
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
from .validate import render_validation_section, stored_validation

#: The bucket roots published acquisitions live under.
DATA_ROOTS = ("sar-data/", "open-data/")

#: How many missing acquisitions the report lists by name (the counts are exact).
MISSING_LIST_LIMIT = 500

#: Publication bar for the weekly snapshot. ``pct_indexable`` is indexed
#: acquisitions over the ones we expect (a sidecar, and not a deduped or
#: quarantined row). The publish workflow imports this rather than hard-coding
#: the number a second time.
COVERAGE_GATE_PCT = 99.0


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


def accounted_group_ids(validation: dict[str, Any] | None) -> set[str]:
    """Bucket groups the index dropped on purpose, from a validation report.

    ``validation`` is the object ``umbra index validate`` writes
    (``index-validation.json``) or the same lists stored on the index:
    ``quarantined`` and ``duplicates`` rows, each with the dropped ``href``.
    A href that is not an open-data acquisition does not account for anything.
    Counts alone are not subtracted — a missing sidecar the report does not
    name stays missing.
    """
    if not isinstance(validation, dict):
        return set()
    groups: set[str] = set()
    for key in ("quarantined", "duplicates"):
        rows = validation.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            href = row.get("href")
            if not isinstance(href, str):
                continue
            bucket_key = _href_to_key(href)
            gid = group_key(bucket_key) if bucket_key else None
            if gid:
                groups.add(gid)
    return groups


def coverage_report(
    index: Any,
    keys: Iterable[str],
    *,
    missing_limit: int = MISSING_LIST_LIMIT,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare a built index against a bucket listing.

    ``index`` is an open :class:`~umbra_py.CatalogIndex`; ``keys`` is every
    object key under the data roots (see :func:`list_bucket_keys`). Coverage is
    counted in acquisitions. ``pct`` is indexed over every acquisition in the
    bucket. ``pct_indexable`` is indexed over acquisitions that have a sidecar
    and were not dropped on purpose (a deduped duplicate or a quarantined row
    in ``validation``, or in the record stored on the index when ``validation``
    is omitted). Those drops are ``accounted``, not missing.
    """
    acqs = bucket_acquisitions(keys)
    indexed = indexed_groups(index)
    record = validation if isinstance(validation, dict) else stored_validation(index)
    omitted = accounted_group_ids(record)
    rows, distinct, captures = index._conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT id), COALESCE(SUM(canonical = 1), 0) FROM items"
    ).fetchone()

    by_prefix: dict[str, dict[str, int]] = {}
    by_layout: dict[str, dict[str, int]] = {}
    by_product: dict[str, dict[str, dict[str, int]]] = {}
    missing: list[dict[str, Any]] = []
    unindexable: list[str] = []
    accounted: list[str] = []
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
            elif gid in omitted:
                accounted.append(gid)
            else:
                missing.append(
                    {"group": gid, "layout": acq.layout, "products": sorted(acq.products)}
                )

    def finish(row: dict[str, int]) -> dict[str, Any]:
        return {**row, "pct": _pct(row["indexed"], row["bucket"])}

    total = len(acqs)
    hit_total = sum(1 for gid in acqs if gid in indexed)
    indexable = total - len(unindexable)
    # Intentional drops are not a hole in the snapshot. The gate reads
    # ``pct_indexable``, so a real missing sidecar is what still lowers it.
    expected = indexable - len(accounted)
    pct_indexable = _pct(hit_total, expected)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "index": {
            "path": str(getattr(index, "path", "")),
            "built_at": index.get_meta("built_at"),
            "rows": rows,
            "distinct_ids": distinct,
            "unique_captures": int(captures or 0),
        },
        "totals": {
            "bucket": total,
            "indexable": indexable,
            "indexed": hit_total,
            "accounted": len(accounted),
            "expected": expected,
            "pct": _pct(hit_total, total),
            "pct_indexable": pct_indexable,
        },
        "gate": {
            "min_pct_indexable": COVERAGE_GATE_PCT,
            "pct_indexable": pct_indexable,
            "meets": pct_indexable >= COVERAGE_GATE_PCT,
        },
        "by_prefix": {k: finish(v) for k, v in sorted(by_prefix.items())},
        "by_layout": {k: finish(v) for k, v in sorted(by_layout.items())},
        "by_product": {
            prefix: {p: finish(per[p]) for p in PRODUCT_ASSETS if p in per}
            for prefix, per in sorted(by_product.items())
        },
        "missing_count": len(missing),
        "missing": missing[:missing_limit],
        "accounted": accounted,
        "unindexable": unindexable,
        # Quarantine / dedupe from the build that wrote this index (PRD P1-2),
        # or the validation document the caller passed. ``None`` on an index
        # that predates that pass and was given no document.
        "validation": record,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """A short human-readable rendering of :func:`coverage_report`."""
    t = report["totals"]
    idx = report["index"]
    lines = [
        "# umbra-py index coverage",
        "",
        f"Generated {report['generated_at']} against index built {idx['built_at']} "
        f"({idx['rows']} rows, {idx['distinct_ids']} distinct ids, "
        f"{idx.get('unique_captures', idx['rows'])} unique captures).",
        "",
        f"**Overall: {t['indexed']} of {t['bucket']} bucket acquisitions indexed "
        f"({t['pct']}%; {_indexable_phrase(t)}).**",
        "",
        _gate_line(report["gate"]),
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
        f"Missing: {report['missing_count']}. "
        f"Accounted (deduped or quarantined): {t['accounted']}. "
        f"Unindexable (no sidecar): {len(report['unindexable'])}.",
        "",
    ]
    counts = _validation_counts(report.get("validation"))
    if counts:
        lines.append(counts)
        lines.append("")
    lines += [
        "## Validation",
        "",
    ]
    validation = report.get("validation")
    if isinstance(validation, dict):
        lines.append(render_validation_section(validation).rstrip())
    else:
        lines.append("No quarantine record on this index.")
    lines += ["", "Contains Umbra open data, licensed under CC BY 4.0.", ""]
    return "\n".join(lines)


def _gate_line(gate: dict[str, Any]) -> str:
    """One sentence so the published markdown shows the 99% bar itself."""
    verdict = "meets" if gate["meets"] else "is below"
    return (
        f"Publication bar: pct_indexable >= {gate['min_pct_indexable']:g}% "
        f"of expected acquisitions. This report {verdict} the bar "
        f"({gate['pct_indexable']}%)."
    )


def _validation_counts(validation: Any) -> str | None:
    """Quarantine and duplicate counts, or ``None`` when the index has no record."""
    if not isinstance(validation, dict):
        return None
    return (
        f"Quarantined: {validation.get('quarantined_count', 0)} "
        f"(each reason is in the table below). "
        f"Duplicates dropped: {validation.get('duplicates_dropped', 0)}."
    )


def _indexable_phrase(totals: dict[str, Any]) -> str:
    """The sidecar half of the headline. Intentional drops get their own clause."""
    if totals.get("accounted"):
        return (
            f"{totals['pct_indexable']}% of the {totals['expected']} expected "
            f"({totals['indexable']} with a sidecar, {totals['accounted']} omitted "
            f"by dedupe or quarantine)"
        )
    return f"{totals['pct_indexable']}% of the {totals['indexable']} with a sidecar"


def write_report(report: dict[str, Any], json_path: str | None, md_path: str | None) -> None:
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    if md_path:
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
