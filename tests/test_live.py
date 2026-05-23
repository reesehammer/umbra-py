"""Live integration tests against Umbra's public catalog.

Skipped by default; run with: ``pytest -m network``.
"""

import pytest

from umbra_py import UmbraCatalog

pytestmark = pytest.mark.network


def test_search_returns_items():
    catalog = UmbraCatalog()
    items = list(catalog.search(start="2024-02-08", end="2024-02-08", limit=3))
    assert items
    for item in items:
        assert item.id
        assert item.available_assets
        assert item.bbox is not None
