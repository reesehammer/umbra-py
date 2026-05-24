"""Live integration tests against Umbra's public catalog.

Skipped by default; run with: ``pytest -m network``.
"""

import pytest

from umbra_py import UmbraCatalog

pytestmark = pytest.mark.network


def test_search_returns_items():
    catalog = UmbraCatalog()
    # The walker issues one paginated LIST per top-level task directory
    # (~80 of them) before yielding anything, so even a single-day search
    # against a real bucket takes tens of seconds. Use a wide window and
    # limit=1 to keep this test bounded -- one item with downloadable data
    # is enough to prove the v2 walker is reaching real acquisitions.
    items = list(catalog.search(start="2024-01-01", end="2024-12-31", limit=1))
    assert items
    item = items[0]
    assert item.id
    assert item.available_assets
    assert item.bbox is not None
    # Every yielded item must have a resolvable public-bucket asset URL.
    href = item.asset_href(item.available_assets[0])
    assert href.startswith("https://")
