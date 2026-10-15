"""Run the replace-your-crawler snippets against production.

The programs under ``docs/examples/replace_crawler/`` are the files the docs
page includes, so this and the manual stay on one copy. HTTP and
``pystac-client`` always run: ``umbra serve`` rewrites asset keys to schema
v2 on the way out. The DuckDB program reads ``assets.gec`` on the published
parquet. That file still has one struct field per filename until the next
``publish-index`` rebuild, so the DuckDB program is skipped while file
metadata ``umbra-py:asset_schema`` is not 2 and the ``assets`` struct has no
``gec`` field. A broken download URL still fails the run.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "docs" / "examples" / "replace_crawler"
HTTP_SNIPPET = SNIPPETS / "get_search.py"
PYSTAC_SNIPPET = SNIPPETS / "pystac_search.py"
DUCKDB_SNIPPET = SNIPPETS / "duckdb_gec.py"

# Stable product keys from asset schema v2 (``umbra_py.models.ASSET_KEYS``).
# ``file`` / ``file_<ext>`` are the fallback for a suffix that matches none of
# those, and ``<key>_2`` is a second file of the same product.
V2_BASE_KEYS = (
    "gec",
    "csi",
    "sidd",
    "csi_sidd",
    "sicd",
    "cphd",
    "metadata",
    "stac_metadata",
    "di_gif",
)
_COLLISION = re.compile(r"^(?P<base>.+)_(?P<n>[0-9]+)$")


def http_constants(source: str) -> list[str]:
    """String constants in ``source``, with implicit concatenations joined."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def search_url(source: str) -> str:
    """The ``/search?...`` URL a snippet builds."""
    for value in http_constants(source):
        if value.startswith("https://api.umbra-py.space/search?"):
            return value
    raise SystemExit("HTTP snippet has no https://api.umbra-py.space/search URL")


def parquet_url(source: str) -> str:
    """The ``umbra-open-data.parquet`` URL a snippet builds."""
    for value in http_constants(source):
        if value.endswith("/catalog-index/umbra-open-data.parquet"):
            return value
    raise SystemExit("DuckDB snippet has no umbra-open-data.parquet URL")


def assert_hrefs(lines: list[str], *, label: str) -> list[str]:
    """Require at least one public HTTPS GeoTIFF href."""
    hrefs = [line.strip() for line in lines if line.strip()]
    if not hrefs:
        raise SystemExit(f"{label}: no GEC hrefs")
    for href in hrefs:
        parts = urlsplit(href)
        if parts.scheme != "https" or not parts.netloc:
            raise SystemExit(f"{label}: not an https href: {href}")
        if "prod-prod-processed" in href:
            raise SystemExit(f"{label}: private href: {href}")
        if not parts.path.endswith(".tif"):
            raise SystemExit(f"{label}: GEC href is not a GeoTIFF: {href}")
    return hrefs


def is_v2_asset_key(key: str) -> bool:
    """Whether ``key`` is a schema-v2 asset key, including ``_2`` / ``file_<ext>``."""
    match = _COLLISION.fullmatch(key)
    base = match.group("base") if match else key
    if base in V2_BASE_KEYS or base == "file":
        return True
    return base.startswith("file_") and base.removeprefix("file_").isalnum()


def assert_v2_features(features: list[dict]) -> list[str]:
    """Require schema v2, a ``gec`` asset, and its filename and href."""
    if not features:
        raise SystemExit("search returned no features")
    hrefs: list[str] = []
    for feature in features:
        version = (feature.get("properties") or {}).get("umbra-py:asset_schema")
        if version != 2:
            raise SystemExit(f"feature {feature.get('id')}: umbra-py:asset_schema is {version!r}")
        assets = feature.get("assets") or {}
        unknown = [key for key in assets if not is_v2_asset_key(str(key))]
        if unknown:
            raise SystemExit(f"feature {feature.get('id')}: asset keys are not v2: {unknown}")
        gec = assets.get("gec")
        if not isinstance(gec, dict):
            raise SystemExit(f"feature {feature.get('id')}: no gec asset")
        filename = gec.get("umbra-py:filename")
        if not isinstance(filename, str) or not filename.strip():
            raise SystemExit(f"feature {feature.get('id')}: gec has no umbra-py:filename")
        href = gec.get("href")
        if not isinstance(href, str):
            raise SystemExit(f"feature {feature.get('id')}: gec has no href")
        hrefs.append(href)
    return assert_hrefs(hrefs, label="search")


def duckdb_v2_ready(metadata: dict | None, asset_field_names: set[str]) -> bool:
    """Whether the published parquet can answer ``assets.gec.href``.

    Schema v2 is the collection field ``umbra-py:asset_schema: 2`` in the
    ``stac-geoparquet`` file metadata, or a struct field named ``gec`` when
    that metadata was not stamped. A v1 file has neither: its asset fields
    are filenames.
    """
    collection = ((metadata or {}).get("collections") or {}).get("umbra-open-data") or {}
    if collection.get("umbra-py:asset_schema") == 2:
        return True
    return "gec" in asset_field_names


def run_snippet(path: Path, *, timeout: int = 180) -> list[str]:
    """Run ``path`` and return its non-empty stdout lines."""
    completed = subprocess.run(
        [sys.executable, str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    print(f"--- {path.name} ---")
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    if completed.returncode != 0:
        raise SystemExit(f"{path.name} exited {completed.returncode}\n{completed.stderr}".rstrip())
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise SystemExit(f"search response was not an object: {url}")
    return payload


def _parquet_probe(url: str) -> tuple[dict | None, set[str]]:
    """Read stac-geoparquet metadata and the asset-struct field names."""
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    raw = con.execute(
        "SELECT value FROM parquet_kv_metadata(?) WHERE key = 'stac-geoparquet'",
        [url],
    ).fetchall()
    metadata = None
    if raw:
        value = raw[0][0]
        text = value.decode() if isinstance(value, bytes) else str(value)
        metadata = json.loads(text)
    names = {
        row[0]
        for row in con.execute(
            "SELECT name FROM parquet_schema(?) WHERE name = 'gec'",
            [url],
        ).fetchall()
    }
    return metadata, names


def main() -> None:
    http_source = HTTP_SNIPPET.read_text(encoding="utf-8")
    url = search_url(http_source)
    hrefs = assert_hrefs(run_snippet(HTTP_SNIPPET), label=HTTP_SNIPPET.name)
    features = _fetch_json(url).get("features") or []
    if hrefs != assert_v2_features(features):
        raise SystemExit("HTTP snippet hrefs differ from assets.gec.href on its search URL")

    pystac_hrefs = assert_hrefs(run_snippet(PYSTAC_SNIPPET), label=PYSTAC_SNIPPET.name)
    if pystac_hrefs != hrefs:
        raise SystemExit("pystac-client hrefs differ from the HTTP snippet")

    parquet = parquet_url(DUCKDB_SNIPPET.read_text(encoding="utf-8"))
    metadata, names = _parquet_probe(parquet)
    if not duckdb_v2_ready(metadata, names):
        print(
            "DuckDB snippet skipped: published umbra-open-data.parquet is still "
            "asset schema v1 (filename keys in the assets struct). The STAC API "
            "already serves v2. This check starts when the next publish-index "
            "rebuild stamps umbra-py:asset_schema 2, or when an assets field "
            "named gec exists."
        )
        return
    assert_hrefs(run_snippet(DUCKDB_SNIPPET, timeout=300), label=DUCKDB_SNIPPET.name)


if __name__ == "__main__":
    main()
