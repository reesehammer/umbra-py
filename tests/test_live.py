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


def test_quicklook_renders_real_cog(tmp_path):
    """End-to-end: search the live bucket, then render one acquisition's GEC
    to a PNG via range requests. Proves the /vsicurl/ read + SAR stretch
    pipeline works against a real cloud-optimized GeoTIFF."""
    pytest.importorskip("rasterio")
    pytest.importorskip("PIL")
    from umbra_py import save_quicklook

    items = list(UmbraCatalog().search(start="2024-01-01", end="2024-12-31", limit=1))
    assert items
    # Keep it small so the test only fetches a low-res overview, not the
    # full multi-gigabyte raster.
    out = save_quicklook(items[0], tmp_path / "quicklook.png", max_size=256, db=True)
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
