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


def test_large_task_paginates_past_1000_keys():
    """Regression for the S3 pagination bug: without ``list-type=2`` the
    lister silently truncated every task at its first 1,000 keys. Centerfield,
    Utah is a task large enough to span multiple pages, so streaming it must
    yield well over 1,000 keys once the ListObjectsV2 protocol is used.
    """
    catalog = UmbraCatalog()
    prefix = "sar-data/tasks/Centerfield, Utah/"
    count = 0
    for _ in catalog._stream_keys(prefix):
        count += 1
        if count > 1000:
            break  # proven multi-page; no need to drain the whole listing
    if count == 0:
        pytest.skip(f"task {prefix!r} not present in the live bucket anymore")
    assert count > 1000, (
        f"only {count} keys under {prefix!r}; pagination is truncating "
        "(is list-type=2 still on the listing URL?)"
    )


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


def test_change_composite_renders_real_task(tmp_path):
    """End-to-end: find an Umbra task with 2+ acquisitions of the same area,
    then render a co-registered change composite from two of them. Proves
    the multi-date warp-to-common-grid + compositing pipeline works against
    real cloud-optimized GeoTIFFs."""
    pytest.importorskip("rasterio")
    pytest.importorskip("PIL")
    from umbra_py import save_change_composite

    # Group a modest sample by task; a task is repeat imaging of one site,
    # so two of its acquisitions are guaranteed to overlap.
    by_task: dict[str, list] = {}
    for item in UmbraCatalog().search(start="2024-01-01", end="2024-12-31", limit=12):
        task = item.properties.get("umbra:task_id")
        if task and "GEC" in item.available_assets:
            by_task.setdefault(task, []).append(item)

    pair = next((v[:2] for v in by_task.values() if len(v) >= 2), None)
    if pair is None:
        pytest.skip("no task with 2+ GEC acquisitions in the sampled window")

    out = save_change_composite(pair, tmp_path / "change.png", max_size=256)
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_change_animation_renders_real_task(tmp_path):
    """End-to-end: co-register a real task's acquisitions into an animated
    time-lapse GIF -- proves the multi-frame warp + GIF assembly works against
    real cloud-optimized GeoTIFFs."""
    pytest.importorskip("rasterio")
    PIL = pytest.importorskip("PIL")  # noqa: N806
    from umbra_py import save_change_animation

    by_task: dict[str, list] = {}
    for item in UmbraCatalog().search(start="2024-01-01", end="2024-12-31", limit=12):
        task = item.properties.get("umbra:task_id")
        if task and "GEC" in item.available_assets:
            by_task.setdefault(task, []).append(item)

    series = next((v for v in by_task.values() if len(v) >= 2), None)
    if series is None:
        pytest.skip("no task with 2+ GEC acquisitions in the sampled window")

    out = save_change_animation(series, tmp_path / "lapse.gif", max_size=256, db=True)
    assert out.exists()
    assert out.read_bytes()[:4] == b"GIF8"
    with PIL.Image.open(out) as im:
        assert getattr(im, "n_frames", 1) >= 1


def test_swipe_map_renders_real_task(tmp_path):
    """End-to-end: build a before/after swipe map from two real acquisitions
    of the same task -- proves both COG overlays stream and embed and the
    side-by-side control is wired into the HTML."""
    pytest.importorskip("rasterio")
    pytest.importorskip("folium")
    pytest.importorskip("PIL")
    from umbra_py import save_swipe_map

    by_task: dict[str, list] = {}
    for item in UmbraCatalog().search(start="2024-01-01", end="2024-12-31", limit=12):
        task = item.properties.get("umbra:task_id")
        if task and "GEC" in item.available_assets:
            by_task.setdefault(task, []).append(item)

    pair = next((v[:2] for v in by_task.values() if len(v) >= 2), None)
    if pair is None:
        pytest.skip("no task with 2+ GEC acquisitions in the sampled window")

    out = save_swipe_map(pair[0], pair[1], tmp_path / "swipe.html", max_size=256, db=True)
    assert out.exists()
    text = out.read_text()
    assert "L.control.sideBySide" in text
    assert text.count("data:image/png;base64,") == 2


def test_fetch_prebuilt_index_from_release(tmp_path):
    """The published catalog-index release serves a downloadable catalog.db
    that `CatalogIndex.from_release` opens into a searchable index -- the
    consume side of the weekly publish workflow, hit against the real release.
    """
    from umbra_py import CatalogIndex
    from umbra_py.exceptions import DownloadError

    dest = tmp_path / "catalog.db"
    try:
        idx = CatalogIndex.from_release(dest)
    except DownloadError as exc:
        # No published snapshot yet (e.g. the weekly publish hasn't run, or is
        # mid-publish). That's a release-availability gap, not the catalog drift
        # this canary exists to catch -- skip rather than raise a false alarm.
        pytest.skip(f"catalog-index release asset not available: {exc}")
    with idx:
        if len(idx) == 0:
            pytest.skip("catalog-index release has no items yet")
        assert dest.exists()
        # A prebuilt snapshot must answer a local search without any live walk.
        first = next(idx.search(limit=1), None)
        assert first is not None
        assert first.id
