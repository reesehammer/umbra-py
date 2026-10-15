"""The replace-crawler docs, snippets, and daily canary stay on one copy.

``docs/replace-crawler.md`` includes the programs in
``docs/examples/replace_crawler/``. The workflow runs
``scripts/replace_crawler_canary.py``, which executes those files. These
tests lock that wiring, the query parameters checked against ``/search``,
and the v2 key rules. They do not call the network.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import yaml

from umbra_py.models import ASSET_KEYS

ROOT = Path(__file__).resolve().parents[1]
SNIPPETS = ROOT / "docs" / "examples" / "replace_crawler"
PAGE = ROOT / "docs" / "replace-crawler.md"
WORKFLOW = ROOT / ".github" / "workflows" / "replace-crawler-canary.yml"
PARQUET_URL = (
    "https://github.com/reesehammer/umbra-py/releases/download/"
    "catalog-index/umbra-open-data.parquet"
)


def _load_canary():
    path = ROOT / "scripts" / "replace_crawler_canary.py"
    spec = importlib.util.spec_from_file_location("replace_crawler_canary", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


canary = _load_canary()


def test_snippet_filenames_do_not_shadow_imports():
    """Running a snippet puts its directory first on sys.path.

    A file named ``http.py`` is imported as the stdlib ``http`` package, and
    ``pystac_client.py`` hides the installed client. The names stay off those
    modules.
    """
    names = {path.stem for path in SNIPPETS.glob("*.py")}
    assert names.isdisjoint({"http", "urllib", "json", "duckdb", "pystac_client"})


def test_docs_include_the_snippet_files_the_canary_runs():
    page = PAGE.read_text(encoding="utf-8")
    for path in (canary.HTTP_SNIPPET, canary.PYSTAC_SNIPPET, canary.DUCKDB_SNIPPET):
        assert path.is_file(), path
        rel = path.relative_to(ROOT).as_posix()
        assert f'--8<-- "{rel}"' in page
        ast.parse(path.read_text(encoding="utf-8"))
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = document["jobs"]["canary"]["steps"][-2]
    assert step["run"] == "python scripts/replace_crawler_canary.py"


def test_workflow_is_a_daily_dispatchable_canary():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML 1.1 parses a bare `on:` key as boolean true.
    triggers = document["on"] if "on" in document else document[True]
    assert triggers["schedule"][0]["cron"] == "17 11 * * *"
    assert triggers["workflow_dispatch"] is None
    assert document["jobs"]["canary"]["runs-on"] == "ubuntu-24.04"
    assert document["permissions"]["issues"] == "write"
    failure = document["jobs"]["canary"]["steps"][-1]
    assert failure["if"] == "failure()"
    assert 'gh issue create --title "Replace-crawler canary failed"' in failure["run"]
    assert "gh issue comment" in failure["run"]


def test_page_names_every_v2_asset_key_and_the_parquet_url():
    page = PAGE.read_text(encoding="utf-8")
    for key in ASSET_KEYS:
        assert f"`{key}`" in page
    assert "`umbra-py:filename`" in page
    assert PARQUET_URL in page
    assert "https://api.umbra-py.space/" in page


def test_canary_key_list_matches_the_schema():
    assert canary.V2_BASE_KEYS == ASSET_KEYS


def test_snippets_use_the_verified_search():
    http = (SNIPPETS / "get_search.py").read_text(encoding="utf-8")
    assert canary.search_url(http) == (
        "https://api.umbra-py.space/search"
        "?bbox=85.49,28.26,85.56,28.32"
        "&datetime=2026-09-01/.."
        "&product_types=GEC"
        "&collections=umbra-open-data"
        "&limit=5"
    )
    assert '["gec"]' in http

    pystac = (SNIPPETS / "pystac_search.py").read_text(encoding="utf-8")
    assert "bbox=[85.49, 28.26, 85.56, 28.32]" in pystac
    assert 'datetime="2026-09-01/.."' in pystac
    assert '"product_types": {"in": ["GEC"]}' in pystac
    assert 'collections=["umbra-open-data"]' in pystac
    assert 'item.assets["gec"].href' in pystac

    duck = (SNIPPETS / "duckdb_gec.py").read_text(encoding="utf-8")
    assert canary.parquet_url(duck) == PARQUET_URL
    assert "assets.gec.href" in duck
    assert "bbox.xmin" in duck and "bbox.xmax" in duck
    assert "bbox.ymin" in duck and "bbox.ymax" in duck
    assert "CAST(datetime AS DATE) >= DATE '2026-09-01'" in duck


def test_href_and_v2_assertions():
    href = (
        "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
        "sar-data/tasks/x/2026-09-02_GEC.tif"
    )
    assert canary.assert_hrefs([href], label="t") == [href]
    with pytest.raises(SystemExit):
        canary.assert_hrefs([], label="t")
    with pytest.raises(SystemExit):
        canary.assert_hrefs(["http://example.com/a.tif"], label="t")
    with pytest.raises(SystemExit):
        canary.assert_hrefs(
            ["https://example.com/prod-prod-processed/a.tif"],
            label="t",
        )

    assert canary.is_v2_asset_key("gec")
    assert canary.is_v2_asset_key("gec_2")
    assert canary.is_v2_asset_key("csi_sidd")
    assert canary.is_v2_asset_key("file_json")
    assert not canary.is_v2_asset_key("2026-09-02_UMBRA-07_GEC.tif")

    feature = {
        "id": "abc",
        "properties": {"umbra-py:asset_schema": 2},
        "assets": {
            "gec": {"href": href, "umbra-py:filename": "2026-09-02_GEC.tif"},
            "sicd": {"href": "https://example.com/a.nitf"},
        },
    }
    assert canary.assert_v2_features([feature]) == [href]
    broken = {
        "id": "abc",
        "properties": {"umbra-py:asset_schema": 2},
        "assets": {"2026_GEC.tif": {"href": href, "umbra-py:filename": "x.tif"}},
    }
    with pytest.raises(SystemExit):
        canary.assert_v2_features([broken])


def test_duckdb_gate_waits_for_schema_v2():
    assert canary.duckdb_v2_ready({"version": "1.0.0"}, set()) is False
    assert (
        canary.duckdb_v2_ready(
            {"collections": {"umbra-open-data": {"umbra-py:asset_schema": 2}}},
            set(),
        )
        is True
    )
    assert canary.duckdb_v2_ready({"version": "1.0.0"}, {"gec"}) is True
    assert canary.duckdb_v2_ready(None, set()) is False
