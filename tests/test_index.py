"""Offline tests for the local SQLite catalog index."""

from __future__ import annotations

from pathlib import Path

from umbra_py.index import BakedPreview, CatalogIndex, default_index_path
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _make_item(task, acq, item_id, dt, bbox, products=("GEC",)):
    """Build an UmbraItem with a realistic public sidecar href.

    The href encodes the task and the acquisition directory, so the item's
    ``task`` and the index's acquisition date both derive correctly. Asset
    keys are named so ``available_assets`` classifies them as the given
    product types.
    """
    base = f"{_BUCKET}/sar-data/tasks/{task}/{acq}/{acq}"
    href = f"{base}.stac.v2.json"
    assets: dict[str, dict] = {}
    for p in products:
        if p in ("GEC", "CSI"):
            assets[f"{acq}_{p}.tif"] = {
                "href": f"{base}_{p}.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            }
        else:
            assets[f"{acq}_{p}.nitf"] = {
                "href": f"{base}_{p}.nitf",
                "type": "application/vnd.nitf",
            }
    doc = {
        "id": item_id,
        "properties": {"datetime": dt, "sar:product_type": products[0]},
        "bbox": list(bbox),
        "geometry": None,
        "assets": assets,
    }
    return UmbraItem.from_dict(doc, href=href)


# Three acquisitions across two tasks, one out of a typical 2024 window.
_A = _make_item(
    "SiteA",
    "2024-01-15-10-00-00_UMBRA-04",
    "a",
    "2024-01-15T10:00:00Z",
    (0, 0, 1, 1),
    products=("GEC", "SICD"),
)
_B = _make_item(
    "SiteB", "2024-02-10-12-00-00_UMBRA-09", "b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)
)
_C = _make_item("SiteA", "2023-06-01-00-00-00_UMBRA-04", "c", "2023-06-01T00:00:00Z", (5, 5, 6, 6))


def _index(tmp_path, items=(_A, _B, _C)):
    idx = CatalogIndex(tmp_path / "catalog.db")
    for it in items:
        idx.add(it)
    idx.commit()
    return idx


def test_add_and_search_round_trip(tmp_path):
    with _index(tmp_path) as idx:
        ids = {i.id for i in idx.search()}
    assert ids == {"a", "b", "c"}


def test_reconstructed_item_keeps_assets_and_href(tmp_path):
    with _index(tmp_path) as idx:
        [a] = [i for i in idx.search() if i.id == "a"]
    assert a.available_assets == ["GEC", "SICD"]
    href = a.asset_href("GEC")
    assert href.endswith("2024-01-15-10-00-00_UMBRA-04_GEC.tif")
    assert href.startswith("https://")


def test_search_date_range_prunes(tmp_path):
    with _index(tmp_path) as idx:
        ids = {i.id for i in idx.search(start="2024-01-01", end="2024-12-31")}
    # The 2023 acquisition is outside the window.
    assert ids == {"a", "b"}


def test_search_bbox_filter(tmp_path):
    with _index(tmp_path) as idx:
        ids = {i.id for i in idx.search(bbox=(0, 0, 5, 5))}
    # a (0-1) overlaps; c (5-6) touches the edge; b (10-11) does not.
    assert ids == {"a", "c"}


def test_search_product_type_filter(tmp_path):
    with _index(tmp_path) as idx:
        ids = {i.id for i in idx.search(product_types=["SICD"])}
    # Only item a exposes SICD.
    assert ids == {"a"}


def test_search_area_filter_is_case_insensitive(tmp_path):
    with _index(tmp_path) as idx:
        ids = {i.id for i in idx.search(area="sitea")}
    assert ids == {"a", "c"}


def test_search_area_escapes_like_wildcards(tmp_path):
    """An underscore in the query must match literally, not as a wildcard."""
    weird = _make_item(
        "River_Nov", "2024-03-01-00-00-00_UMBRA-04", "w", "2024-03-01T00:00:00Z", (0, 0, 1, 1)
    )
    with _index(tmp_path, items=(_A, weird)) as idx:
        # 'r_v' would match 'River_Nov' if _ were a wildcard; it must not.
        assert {i.id for i in idx.search(area="r_v")} == set()
        assert {i.id for i in idx.search(area="river_nov")} == {"w"}


_CF = _make_item(
    "Centerfield, Utah", "2024-01-15-10-00-00_UMBRA-04", "cf", "2024-01-15T10:00:00Z", (0, 0, 1, 1)
)
_PR = _make_item(
    "Provo, Utah", "2024-02-01-10-00-00_UMBRA-05", "pr", "2024-02-01T10:00:00Z", (2, 2, 3, 3)
)


def test_search_area_fuzzy_matches_word_order_and_typos(tmp_path):
    """fuzzy=True on the index path mirrors the live path's token-wise match."""
    with _index(tmp_path, items=(_CF, _PR)) as idx:
        for query in ("utah centerfield", "centerfield utah", "centrfield"):
            assert {i.id for i in idx.search(area=query, fuzzy=True)} == {"cf"}, query
        # A one-token query that names the shared state matches both tasks.
        assert {i.id for i in idx.search(area="utah", fuzzy=True)} == {"cf", "pr"}


def test_search_area_fuzzy_off_keeps_substring_only(tmp_path):
    """Without fuzzy, a reordered query matches nothing (legacy LIKE behaviour)."""
    with _index(tmp_path, items=(_CF, _PR)) as idx:
        assert {i.id for i in idx.search(area="utah centerfield")} == set()
        # The substring path still works unchanged.
        assert {i.id for i in idx.search(area="centerfield")} == {"cf"}


def test_search_area_fuzzy_no_match_yields_nothing(tmp_path):
    with _index(tmp_path, items=(_CF, _PR)) as idx:
        assert list(idx.search(area="nowhere at all", fuzzy=True)) == []


def test_fuzzy_agrees_across_live_and_index_paths(tmp_path):
    """The two backends must return the same ids for the same fuzzy query."""
    from umbra_py.catalog import UmbraCatalog

    items = (_CF, _PR)

    # Live path: stub the catalog to yield these items grouped by task.
    by_task: dict[str, list] = {}
    for it in items:
        by_task.setdefault(it.task, []).append(it)
    prefixes = [f"sar-data/tasks/{t}/" for t in by_task]

    cat = UmbraCatalog()
    cat._list_prefix = lambda prefix: (prefixes, [])  # type: ignore[assignment]
    cat._walk_task = (
        lambda prefix, start, end: iter(  # type: ignore[assignment]
            by_task[prefix[len("sar-data/tasks/") :].rstrip("/")]
        )
    )

    with _index(tmp_path, items=items) as idx:
        for query in ("utah centerfield", "centrfield", "utah", "provo"):
            live = {i.id for i in cat.search(area=query, fuzzy=True)}
            indexed = {i.id for i in idx.search(area=query, fuzzy=True)}
            assert live == indexed, query


def test_search_limit(tmp_path):
    with _index(tmp_path) as idx:
        assert len(list(idx.search(limit=1))) == 1


def test_search_max_per_task(tmp_path):
    # SiteA has two acquisitions (a, c); SiteB has one (b).
    with _index(tmp_path) as idx:
        items = list(idx.search(max_per_task=1))
    assert len(items) == 2
    assert {i.task for i in items} == {"SiteA", "SiteB"}


def test_get_returns_item_by_id(tmp_path):
    with _index(tmp_path) as idx:
        item = idx.get("a")
    assert item is not None
    assert item.id == "a"
    assert item.task == "SiteA"
    # A keyed lookup reconstructs the full item (assets + href), like search.
    assert item.available_assets == ["GEC", "SICD"]
    assert item.asset_href("GEC").endswith("2024-01-15-10-00-00_UMBRA-04_GEC.tif")


def test_get_missing_id_returns_none(tmp_path):
    with _index(tmp_path) as idx:
        assert idx.get("nope") is None


def test_get_uses_the_id_index(tmp_path):
    # The keyed lookup rides an index; adding it is additive (no schema bump),
    # so a legacy or reopened database gains it too.
    import sqlite3

    path = tmp_path / "catalog.db"
    _index(tmp_path).close()
    names = {
        row[0]
        for row in sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    assert "idx_items_id" in names


def test_add_is_idempotent_upsert(tmp_path):
    idx = CatalogIndex(tmp_path / "catalog.db")
    idx.add(_A)
    idx.add(_A)  # same href -> replace, not duplicate
    idx.commit()
    assert len(idx) == 1
    # Re-adding with a different product set refreshes the asset rows.
    updated = _make_item(
        "SiteA",
        "2024-01-15-10-00-00_UMBRA-04",
        "a",
        "2024-01-15T10:00:00Z",
        (0, 0, 1, 1),
        products=("GEC",),
    )
    idx.add(updated)
    idx.commit()
    assert {i.id for i in idx.search(product_types=["SICD"])} == set()
    idx.close()


def test_index_persists_across_reopen(tmp_path):
    path = tmp_path / "catalog.db"
    with CatalogIndex(path) as idx:
        idx.add(_A)
    with CatalogIndex(path) as idx:
        assert len(idx) == 1
        assert {i.id for i in idx.search()} == {"a"}


def test_stats(tmp_path):
    with _index(tmp_path) as idx:
        s = idx.stats()
    assert s["items"] == 3
    assert s["start"] == "2023-06-01"
    assert s["end"] == "2024-02-10"
    assert s["tasks"] == 2


def test_build_from_catalog(tmp_path):
    """build() consumes catalog.search() and persists each item."""

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([_A, _B, _C])

    with CatalogIndex(tmp_path / "catalog.db") as idx:
        written = idx.build(FakeCatalog())
        assert written == 3
        assert {i.id for i in idx.search()} == {"a", "b", "c"}


def test_build_reports_progress(tmp_path):
    """build(progress=...) reports the running count, ending at the total."""

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([_A, _B, _C])

    seen: list[int] = []
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.build(FakeCatalog(), progress=seen.append)
    assert seen == [1, 2, 3]


def test_build_stamps_built_at(tmp_path):
    """build() records today's date so `index info` can report staleness."""
    from datetime import date

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([_A, _B])

    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.build(FakeCatalog())
        assert idx.get_meta("built_at") == date.today().isoformat()
        assert idx.stats()["built_at"] == date.today().isoformat()


def test_meta_round_trip_and_missing(tmp_path):
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        assert idx.get_meta("built_at") is None
        idx.set_meta("built_at", "2026-07-01")
        idx.set_meta("built_at", "2026-07-08")  # upsert, not duplicate
        assert idx.get_meta("built_at") == "2026-07-08"


def test_from_release_downloads_and_opens(tmp_path):
    """from_release() fetches the published .db and opens a working index."""
    import responses

    # A real, populated SQLite index serialized to bytes stands in for the
    # asset the publish workflow uploads to the catalog-index release.
    src = tmp_path / "published.db"
    with CatalogIndex(src) as built:
        for it in (_A, _B, _C):
            built.add(it)
    payload = src.read_bytes()

    url = "https://example.com/catalog-index/catalog.db"
    dest = tmp_path / "fetched" / "catalog.db"

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        with CatalogIndex.from_release(dest, url=url) as idx:
            return {i.id for i in idx.search()}

    assert run() == {"a", "b", "c"}
    assert dest.exists()


def test_from_release_overwrites_existing(tmp_path):
    """A re-fetch replaces an older snapshot at the same path."""
    import responses

    dest = tmp_path / "catalog.db"
    dest.write_bytes(b"stale-not-a-db")

    fresh = tmp_path / "fresh.db"
    with CatalogIndex(fresh) as built:
        built.add(_A)
    payload = fresh.read_bytes()

    url = "https://example.com/catalog.db"

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        with CatalogIndex.from_release(dest, url=url) as idx:
            return {i.id for i in idx.search()}

    assert run() == {"a"}


def test_default_index_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("UMBRA_INDEX_DB", str(target))
    assert default_index_path() == target


def test_default_index_path_uses_xdg_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("UMBRA_INDEX_DB", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_index_path() == tmp_path / "umbra-py" / "catalog.db"


def test_cli_index_build_then_search_local(tmp_path, monkeypatch):
    """`umbra index build` populates the DB and `umbra search --local` reads it,
    without ever walking S3 live."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py import index as index_mod

    class FakeCatalog:
        def search(self, **kwargs):
            return iter([_A, _B])

    # index.build() with no catalog constructs UmbraCatalog() in the index module.
    monkeypatch.setattr(index_mod, "UmbraCatalog", lambda *a, **k: FakeCatalog())

    db = str(tmp_path / "catalog.db")
    runner = CliRunner()

    built = runner.invoke(cli_mod.cli, ["index", "build", "--db", db])
    assert built.exit_code == 0, built.output
    assert "Indexed 2 acquisition(s)" in built.output

    found = runner.invoke(cli_mod.cli, ["search", "--local", "--db", db])
    assert found.exit_code == 0, found.output
    assert "2 item(s)." in found.output

    info = runner.invoke(cli_mod.cli, ["index", "info", "--db", db])
    assert info.exit_code == 0, info.output
    assert "items : 2" in info.output


def test_cli_index_fetch_then_search_local(tmp_path):
    """`umbra index fetch` downloads the published .db and `search --local`
    reads it, without any live crawl."""
    import responses
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "published.db"
    with CatalogIndex(src) as built:
        for it in (_A, _B):
            built.add(it)
        built.set_meta("built_at", "2026-07-01")
    payload = src.read_bytes()

    url = "https://example.com/catalog-index/catalog.db"
    db = str(tmp_path / "fetched.db")
    runner = CliRunner()

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        return runner.invoke(cli_mod.cli, ["index", "fetch", "--db", db, "--url", url])

    fetched = run()
    assert fetched.exit_code == 0, fetched.output
    assert "Fetched prebuilt index: 2 acquisition(s), built 2026-07-01" in fetched.output

    found = runner.invoke(cli_mod.cli, ["search", "--local", "--db", db])
    assert found.exit_code == 0, found.output
    assert "2 item(s)." in found.output

    info = runner.invoke(cli_mod.cli, ["index", "info", "--db", db])
    assert info.exit_code == 0, info.output
    assert "built : 2026-07-01" in info.output


def test_cli_index_info_built_unknown(tmp_path):
    """An index with no build stamp reports an honest 'unknown'."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)

    info = CliRunner().invoke(cli_mod.cli, ["index", "info", "--db", str(db)])
    assert info.exit_code == 0, info.output
    assert "built : unknown" in info.output


def test_cli_search_local_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = str(tmp_path / "missing.db")
    result = CliRunner().invoke(cli_mod.cli, ["search", "--local", "--db", db])
    assert result.exit_code != 0
    assert "No index" in result.output


def _no_live_walk(*_a, **_k):
    """Stand-in for UmbraCatalog.search that fails if a command walks S3 while
    it was told to read the local index."""
    raise AssertionError("live S3 walk happened despite --local")


def test_cli_map_local_reads_index_without_walking_s3(tmp_path, monkeypatch):
    """`umbra map --local` renders from a prebuilt index and never touches S3."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    with _index(tmp_path, items=(_A, _B)):
        pass
    db = str(tmp_path / "catalog.db")

    # Any live walk is a bug when --local is set: make it explode.
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", _no_live_walk)

    out = tmp_path / "map.geojson"
    result = CliRunner().invoke(
        cli_mod.cli, ["map", "--local", "--index-db", db, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert '"a"' in text and '"b"' in text
    assert "Wrote 2 footprint(s)" in result.output


def test_cli_map_local_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = str(tmp_path / "missing.db")
    result = CliRunner().invoke(
        cli_mod.cli, ["map", "--local", "--index-db", db, "--out", str(tmp_path / "m.geojson")]
    )
    assert result.exit_code != 0
    assert "No index" in result.output


def test_cli_gallery_local_reads_index(tmp_path, monkeypatch):
    """`umbra gallery --local` streams thumbnails for items pulled from the
    index, without a live S3 walk."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.viz import contact_sheet as viz_mod

    with _index(tmp_path, items=(_A, _B)):
        pass
    db = str(tmp_path / "catalog.db")

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", lambda *_a, **_k: "data:image/png;base64,Z")
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", _no_live_walk)

    out = tmp_path / "gallery.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--local", "--index-db", db, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote gallery of 2 acquisition(s)" in result.output
    assert "data:image/png;base64,Z" in out.read_text()


def test_cli_gallery_local_uses_baked_thumbnails(tmp_path, monkeypatch):
    """`umbra gallery --local` embeds thumbnails already baked into the index
    (umbra index bake-thumbnails) straight from local bytes -- no S3 stream at
    all when every tile is baked."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.viz import contact_sheet as viz_mod

    # Build an index and bake fake PNG bytes with an injectable renderer (no
    # rasterio, no network) -- the same primitive umbra index bake-thumbnails uses.
    with _index(tmp_path, items=(_A, _B)) as idx:
        idx.bake_thumbnails(renderer=lambda it: b"\x89PNG-" + it.id.encode())
        idx.commit()
    db = str(tmp_path / "catalog.db")

    # Streaming any thumbnail is a bug here -- every tile is baked.
    def boom(*_a, **_k):
        raise AssertionError("streamed a baked thumbnail")

    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", boom)
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", _no_live_walk)

    out = tmp_path / "gallery.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--local", "--index-db", db, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "from baked thumbnails" in result.output
    text = out.read_text()
    assert viz_mod._png_data_uri(b"\x89PNG-a") in text
    assert viz_mod._png_data_uri(b"\x89PNG-b") in text


# -- incremental update ---------------------------------------------------------


class _RecordingCatalog:
    """FakeCatalog that records the kwargs of its last search() and returns a
    fixed set of items -- lets a test assert the derived date bound and scope."""

    def __init__(self, items):
        self._items = list(items)
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._items)


# A newer acquisition than any in (_A, _B, _C), for the "one new pass" case.
_D = _make_item(
    "SiteB", "2024-03-01-08-00-00_UMBRA-09", "d", "2024-03-01T08:00:00Z", (10, 10, 11, 11)
)


def test_update_derives_bound_from_newest_indexed(tmp_path):
    """update() walks from (max indexed acq_date - overlap_days)."""
    from datetime import date

    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:  # max acq_date is 2024-02-10 (_B)
        idx.update(cat, overlap_days=0)
    assert cat.calls[0]["start"] == date(2024, 2, 10)


def test_update_overlap_days_widens_the_bound(tmp_path):
    from datetime import date

    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        idx.update(cat, overlap_days=5)
    assert cat.calls[0]["start"] == date(2024, 2, 5)


def test_update_counts_new_vs_refreshed(tmp_path):
    """A returned item already present is 'refreshed'; an unseen one is 'new'."""
    cat = _RecordingCatalog([_B, _D])  # _B already indexed, _D is new
    with _index(tmp_path) as idx:  # holds a, b, c
        result = idx.update(cat, overlap_days=0)
        assert (result.scanned, result.added, result.refreshed) == (2, 1, 1)
        assert {i.id for i in idx.search()} == {"a", "b", "c", "d"}


def test_update_empty_index_falls_back_to_full_build(tmp_path):
    """With nothing indexed there is no bound to derive, so start is None."""
    cat = _RecordingCatalog([_A, _B, _C])
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        result = idx.update(cat)
    assert cat.calls[0]["start"] is None
    assert result.start is None
    assert (result.added, result.refreshed) == (3, 0)


def test_update_since_overrides_derived_bound(tmp_path):
    from datetime import date

    cat = _RecordingCatalog([_A])
    with _index(tmp_path) as idx:
        result = idx.update(cat, since="2020-01-01")
    assert cat.calls[0]["start"] == date(2020, 1, 1)
    assert result.start == date(2020, 1, 1)


def test_update_passes_scope_through(tmp_path):
    """Extra filters (area/bbox/limit) reach the walk unchanged."""
    cat = _RecordingCatalog([])
    with _index(tmp_path) as idx:
        idx.update(cat, overlap_days=0, area="SiteB", limit=5)
    call = cat.calls[0]
    assert call["area"] == "SiteB"
    assert call["limit"] == 5


def test_update_rejects_start_kwarg(tmp_path):
    import pytest

    cat = _RecordingCatalog([])
    with _index(tmp_path) as idx, pytest.raises(TypeError, match="since="):
        idx.update(cat, start="2024-01-01")


def test_update_stamps_built_at(tmp_path):
    from datetime import date

    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        idx.update(cat, overlap_days=0)
        assert idx.get_meta("built_at") == date.today().isoformat()


# -- read-through search (index + live delta) -----------------------------------


def test_search_live_walks_only_from_the_freshness_horizon(tmp_path):
    """The live delta walk starts at (max indexed acq_date - overlap_days)."""
    from datetime import date

    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:  # newest indexed acq_date is 2024-02-10 (_B)
        list(idx.search_live(cat, overlap_days=0))
    assert cat.calls[0]["start"] == date(2024, 2, 10)


def test_search_live_merges_index_and_new_live_items(tmp_path):
    """Results are the union of what's indexed and the new live delta."""
    cat = _RecordingCatalog([_B, _D])  # _B already indexed, _D is new
    with _index(tmp_path) as idx:  # holds a, b, c
        found = list(idx.search_live(cat, overlap_days=0))
    assert {i.id for i in found} == {"a", "b", "c", "d"}


def test_search_live_forwards_acquisition_filters_to_both_streams(tmp_path):
    """The polarization / incidence / resolution filters reach both the index
    query and the live delta walk, so the read-through path filters like a plain
    search on either side."""
    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        list(
            idx.search_live(
                cat,
                overlap_days=0,
                polarizations=["VV"],
                min_incidence=20.0,
                max_incidence=40.0,
                max_resolution=0.5,
            )
        )
    live_kwargs = cat.calls[0]
    assert live_kwargs["polarizations"] == ["VV"]
    assert live_kwargs["min_incidence"] == 20.0
    assert live_kwargs["max_incidence"] == 40.0
    assert live_kwargs["max_resolution"] == 0.5


def test_search_live_deduplicates_overlap_by_href(tmp_path):
    """An acquisition present in both the index and the live delta yields once."""
    cat = _RecordingCatalog([_B])  # _B is already indexed -> a pure overlap
    with _index(tmp_path) as idx:
        ids = [i.id for i in idx.search_live(cat, overlap_days=0)]
    assert ids.count("b") == 1
    assert set(ids) == {"a", "b", "c"}


def test_search_live_caches_new_items_by_default(tmp_path):
    """refresh=True upserts the new delta so a later plain search finds it."""
    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        list(idx.search_live(cat, overlap_days=0))
        assert {i.id for i in idx.search()} == {"a", "b", "c", "d"}


def test_search_live_refresh_false_leaves_index_untouched(tmp_path):
    """refresh=False returns the merged view but does not write the delta back."""
    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        found = {i.id for i in idx.search_live(cat, overlap_days=0, refresh=False)}
        assert found == {"a", "b", "c", "d"}  # _D is in the returned view
        assert {i.id for i in idx.search()} == {"a", "b", "c"}  # but not persisted


def test_search_live_delta_starts_at_horizon_when_caller_start_is_older(tmp_path):
    """The live walk starts at the later of (horizon-overlap, caller start).

    The caller's ``start`` (2024-02-01) is older than the horizon (2024-02-10),
    so the index already covers that span and the live delta only walks from the
    horizon forward -- while the index side still honors the caller's start.
    """
    from datetime import date

    cat = _RecordingCatalog([])
    with _index(tmp_path) as idx:  # horizon 2024-02-10
        found = list(idx.search_live(cat, start="2024-02-01", overlap_days=0))
    assert cat.calls[0]["start"] == date(2024, 2, 10)
    # _A (2024-01-15) and _C (2023) are before the caller's start, so the index
    # side drops them; only _B (2024-02-10) is in the caller's window.
    assert {i.id for i in found} == {"b"}


def test_search_live_delta_uses_caller_start_when_it_is_newer(tmp_path):
    """A caller start newer than the horizon bounds the live walk (never older)."""
    from datetime import date

    cat = _RecordingCatalog([])
    with _index(tmp_path) as idx:  # horizon 2024-02-10
        list(idx.search_live(cat, start="2024-06-01", overlap_days=0))
    assert cat.calls[0]["start"] == date(2024, 6, 1)


def test_search_live_applies_filters_and_limit(tmp_path):
    """Standard filters and limit apply to the merged, de-duplicated stream."""
    cat = _RecordingCatalog([_D])
    with _index(tmp_path) as idx:
        found = list(idx.search_live(cat, overlap_days=0, area="SiteB"))
    assert {i.id for i in found} == {"b", "d"}  # both SiteB passes, none from SiteA


def test_search_live_empty_index_walks_full_window_and_seeds(tmp_path):
    """With nothing indexed the live walk covers the whole request (a first build)."""
    cat = _RecordingCatalog([_A, _B, _C])
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        found = {i.id for i in idx.search_live(cat)}
        assert cat.calls[0]["start"] is None  # no horizon to prune against
        assert found == {"a", "b", "c"}
        assert {i.id for i in idx.search()} == {"a", "b", "c"}  # seeded


def test_cli_search_live_reads_index_and_delta(tmp_path, monkeypatch):
    """`umbra search --local --live` merges the index with a fresh live pass."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    with _index(tmp_path):  # holds a, b, c
        pass
    db = str(tmp_path / "catalog.db")

    def fake_search(self, **kwargs):
        return iter([_D])  # one new pass from the live delta

    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)
    result = CliRunner().invoke(cli_mod.cli, ["search", "--local", "--live", "--db", db])
    assert result.exit_code == 0, result.output
    assert "4 item(s)." in result.output


def test_cli_search_live_requires_local(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(cli_mod.cli, ["search", "--live"])
    assert result.exit_code != 0
    assert "only applies" in result.output


def test_cli_index_update_requires_existing_index(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["index", "update", "--db", str(tmp_path / "missing.db")]
    )
    assert result.exit_code != 0
    assert "No index" in result.output


def test_cli_index_update_refreshes_and_reports(tmp_path, monkeypatch):
    """`umbra index update` walks from the derived bound and prints the tally."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    with _index(tmp_path):  # holds a, b, c; newest acq_date 2024-02-10
        pass
    db = str(tmp_path / "catalog.db")

    def fake_search(self, **kwargs):
        # Only the new pass is returned, as a real walk from the bound would.
        return iter([_D])

    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)
    result = CliRunner().invoke(cli_mod.cli, ["index", "update", "--db", db, "--overlap-days", "0"])
    assert result.exit_code == 0, result.output
    assert "1 new" in result.output
    assert "index now holds 4" in result.output


# -- schema versioning -------------------------------------------------------


def test_fresh_index_stamps_schema_version(tmp_path):
    """A newly created database records the current schema version."""
    import sqlite3

    from umbra_py.index import _SCHEMA_VERSION

    path = tmp_path / "catalog.db"
    with CatalogIndex(path):
        pass
    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


def test_reopen_current_version_preserves_rows(tmp_path):
    """Re-opening a same-version index keeps its data and version stamp."""
    import sqlite3

    from umbra_py.index import _SCHEMA_VERSION

    path = tmp_path / "catalog.db"
    with _index(path.parent):  # writes catalog.db with a, b, c
        pass
    with CatalogIndex(path) as idx:  # second open must not wipe or re-stamp
        assert {i.id for i in idx.search()} == {"a", "b", "c"}
    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


def test_legacy_unversioned_index_is_adopted(tmp_path):
    """A pre-versioning database (user_version 0) is stamped, not rejected.

    Databases built before schema versioning -- including a fetched snapshot --
    read ``user_version == 0`` but already have exactly the version-1 layout, so
    opening them must adopt them in place without losing rows.
    """
    import sqlite3

    from umbra_py.index import _SCHEMA, _SCHEMA_VERSION

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)  # schema, but deliberately no PRAGMA user_version
    conn.execute(
        "INSERT INTO items (href, id, doc) VALUES (?, ?, ?)",
        ("h", "old", '{"id": "old", "assets": {}}'),
    )
    conn.commit()
    conn.close()
    assert sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0] == 0

    with CatalogIndex(path) as idx:
        assert {i.id for i in idx.search()} == {"old"}
    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


def test_newer_schema_version_is_rejected(tmp_path):
    """A database written by a newer umbra-py raises rather than misreading."""
    import sqlite3

    import pytest

    from umbra_py.exceptions import IndexSchemaError
    from umbra_py.index import _SCHEMA_VERSION

    path = tmp_path / "catalog.db"
    with CatalogIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(IndexSchemaError) as exc:
        CatalogIndex(path)
    assert str(_SCHEMA_VERSION + 1) in str(exc.value)


def test_older_schema_version_is_rejected(tmp_path):
    """A lower non-zero version is an un-migratable older schema; it raises.

    Version 1 is the first stamp, so this branch is unreachable today; the test
    forces a synthetic in-between stamp so the guard is exercised and stays
    correct when the schema version is bumped.
    """
    import sqlite3

    import pytest

    from umbra_py.exceptions import IndexSchemaError
    from umbra_py.index import _SCHEMA_VERSION

    path = tmp_path / "catalog.db"
    with CatalogIndex(path):
        pass
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")  # a real, current stamp...
    conn.commit()
    conn.close()

    # ...then monkeypatch the module constant upward so the on-disk stamp reads
    # as an older schema the running build no longer matches.
    import umbra_py.index as index_mod

    original = index_mod._SCHEMA_VERSION
    index_mod._SCHEMA_VERSION = original + 1
    try:
        with pytest.raises(IndexSchemaError) as exc:
            CatalogIndex(path)
        assert "older schema" in str(exc.value)
    finally:
        index_mod._SCHEMA_VERSION = original


# -- baked place labels (bake_places / item.place) ---------------------------


def _counting_geocoder():
    """A deterministic reverse-geocoder that records its call arguments.

    Returns ``(fn, calls)`` where ``fn(lat, lon)`` yields a stable label and
    appends ``(lat, lon)`` to ``calls`` -- so a test can assert both what a bake
    produced and how many items it actually geocoded.
    """
    calls: list[tuple[float, float]] = []

    def fn(lat: float, lon: float) -> str | None:
        calls.append((lat, lon))
        return f"Place@{lat:.1f},{lon:.1f}"

    return fn, calls


def test_bake_places_labels_items_on_search(tmp_path):
    """bake_places geocodes footprint centroids and search yields item.place."""
    geo, calls = _counting_geocoder()
    with _index(tmp_path) as idx:
        labelled = idx.bake_places(geocoder=geo)
        assert labelled == 3
        assert len(calls) == 3
        places = {i.id: i.place for i in idx.search()}
    # _A bbox (0,0,1,1) -> centroid (0.5, 0.5); label is (lat, lon).
    assert places["a"] == "Place@0.5,0.5"
    assert places["b"] == "Place@10.5,10.5"
    assert places["c"] == "Place@5.5,5.5"


def test_bake_places_is_idempotent(tmp_path):
    """A second bake only geocodes items that were not labelled yet."""
    geo, calls = _counting_geocoder()
    with _index(tmp_path) as idx:
        idx.bake_places(geocoder=geo)
        calls.clear()
        # Nothing new to label -> no geocoder calls, no new labels.
        assert idx.bake_places(geocoder=geo) == 0
        assert calls == []
        # A newly added item is the only one the next bake touches.
        idx.add(
            _make_item(
                "SiteD",
                "2024-03-01-00-00-00_UMBRA-04",
                "d",
                "2024-03-01T00:00:00Z",
                (20, 20, 21, 21),
            )
        )
        idx.commit()
        assert idx.bake_places(geocoder=geo) == 1
        assert calls == [(20.5, 20.5)]


def test_bake_places_respects_limit(tmp_path):
    geo, calls = _counting_geocoder()
    with _index(tmp_path) as idx:
        assert idx.bake_places(geocoder=geo, limit=2) == 2
        assert len(calls) == 2
        # The rest are picked up on a later run (idempotent continuation).
        assert idx.bake_places(geocoder=geo) == 1


def _site_passes(task, bboxes, prefix):
    """Repeat passes over one site: same task, near-identical footprints."""
    return [
        _make_item(
            task,
            f"2024-05-{n + 1:02d}-00-00-00_UMBRA-04",
            f"{prefix}{n}",
            f"2024-05-{n + 1:02d}T00:00:00Z",
            bbox,
        )
        for n, bbox in enumerate(bboxes)
    ]


def test_bake_places_by_site_geocodes_once_per_site(tmp_path):
    """One lookup per site labels every pass over it -- the whole-catalog mode."""
    passes = _site_passes(
        "SiteRepeat",
        [(0, 0, 1, 1), (0.01, 0.01, 1.01, 1.01), (0.02, 0.02, 1.02, 1.02)],
        "r",
    )
    geo, calls = _counting_geocoder()
    with _index(tmp_path, items=(*passes, _B)) as idx:
        # Four acquisitions, but only two sites -> two throttled lookups.
        assert idx.bake_places(geocoder=geo, by_site=True) == 4
        assert len(calls) == 2
        places = {i.id: i.place for i in idx.search()}
    # Every pass takes the one label, resolved from the group's mean centroid.
    assert places["r0"] == places["r1"] == places["r2"] == "Place@0.5,0.5"
    assert places["b"] == "Place@10.5,10.5"


def test_bake_places_by_site_separates_distant_passes_of_one_task(tmp_path):
    """A task whose footprints are far apart is still geocoded per location."""
    geo, calls = _counting_geocoder()
    # _A and _C share the task "SiteA" but sit ~5 degrees apart.
    with _index(tmp_path, items=(_A, _C)) as idx:
        assert idx.bake_places(geocoder=geo, by_site=True) == 2
        assert len(calls) == 2
        places = {i.id: i.place for i in idx.search()}
    assert places["a"] != places["c"]


def test_bake_places_by_site_groups_only_within_a_task(tmp_path):
    """Two sites that happen to share a cell keep their own labels."""
    neighbours = [
        _make_item(
            "SiteX", "2024-06-01-00-00-00_UMBRA-04", "x", "2024-06-01T00:00:00Z", (0, 0, 1, 1)
        ),
        _make_item(
            "SiteY", "2024-06-02-00-00-00_UMBRA-04", "y", "2024-06-02T00:00:00Z", (0, 0, 1, 1)
        ),
    ]
    geo, calls = _counting_geocoder()
    with _index(tmp_path, items=neighbours) as idx:
        assert idx.bake_places(geocoder=geo, by_site=True) == 2
        assert len(calls) == 2


def test_bake_places_by_site_limit_caps_lookups(tmp_path):
    """--limit bounds the geocode calls, not the items each call labels."""
    # "SiteAlpha" sorts before "SiteB", so it is the group the cap keeps
    # (grouping follows the href order the rows are read in).
    passes = _site_passes("SiteAlpha", [(0, 0, 1, 1), (0.01, 0.01, 1.01, 1.01)], "r")
    geo, calls = _counting_geocoder()
    with _index(tmp_path, items=(*passes, _B)) as idx:
        # One lookup, but it labels both passes of that site.
        assert idx.bake_places(geocoder=geo, by_site=True, limit=1) == 2
        assert len(calls) == 1
        # The remaining site is picked up on a later run.
        assert idx.bake_places(geocoder=geo, by_site=True) == 1


def test_bake_places_skips_items_without_bbox(tmp_path):
    """An item with no footprint can't be geocoded and is left unlabelled."""
    no_bbox = _make_item(
        "SiteE", "2024-04-01-00-00-00_UMBRA-04", "e", "2024-04-01T00:00:00Z", (0, 0, 1, 1)
    )
    no_bbox.bbox = None  # drop the footprint the centroid needs
    geo, calls = _counting_geocoder()
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        idx.add(no_bbox)
        idx.commit()
        assert idx.bake_places(geocoder=geo) == 0
        assert calls == []
        [e] = list(idx.search())
        assert e.place is None


def test_bake_places_retries_unresolved_items(tmp_path):
    """An item whose geocode returns None stays NULL and is retried next run."""
    with _index(tmp_path, items=(_A,)) as idx:
        assert idx.bake_places(geocoder=lambda lat, lon: None) == 0
        assert next(iter(idx.search())).place is None
        # A later bake with a working geocoder still labels it.
        assert idx.bake_places(geocoder=lambda lat, lon: "Somewhere") == 1
        assert next(iter(idx.search())).place == "Somewhere"


def test_add_preserves_baked_place_on_reindex(tmp_path):
    """Re-indexing an acquisition refreshes its STAC data but keeps the label.

    A weekly `umbra index update` re-reads sidecars; it must not clear a label a
    prior `umbra index bake` computed, since the label is keyed on the footprint,
    not the STAC document.
    """
    with _index(tmp_path, items=(_A,)) as idx:
        idx.bake_places(geocoder=lambda lat, lon: "Baked City")
        # Re-add the same href (the update path's upsert).
        idx.add(_A)
        idx.commit()
        [a] = list(idx.search())
    assert a.place == "Baked City"


def test_stats_reports_labeled_count(tmp_path):
    with _index(tmp_path) as idx:
        assert idx.stats()["labeled"] == 0
        idx.bake_places(geocoder=lambda lat, lon: "X", limit=2)
        assert idx.stats()["labeled"] == 2


def test_get_populates_place(tmp_path):
    with _index(tmp_path) as idx:
        idx.bake_places(geocoder=lambda lat, lon: "Keyed Place")
        got = idx.get("a")
    assert got is not None and got.place == "Keyed Place"


def test_place_column_migration_from_v1(tmp_path):
    """A version-1 index (no place column) is migrated in place, not rejected.

    This is the first real additive migration the schema-versioning was landed
    to enable: opening a v1 database adds the `place` column, stamps version 2,
    and preserves every row -- and a bake then works against it.
    """
    import sqlite3

    from umbra_py.index import _SCHEMA_VERSION

    # The version-1 schema: exactly today's `items` table minus the `place`
    # column added in version 2.
    v1_schema = """
    CREATE TABLE items (
        href TEXT PRIMARY KEY, id TEXT NOT NULL, task TEXT, datetime TEXT,
        acq_date TEXT, min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
        doc TEXT NOT NULL
    );
    CREATE TABLE item_assets (href TEXT NOT NULL, asset TEXT NOT NULL,
        PRIMARY KEY (href, asset));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(v1_schema)
    conn.execute(
        "INSERT INTO items (href, id, doc, min_lon, min_lat, max_lon, max_lat) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("h", "old", '{"id": "old", "assets": {}}', 0.0, 0.0, 2.0, 2.0),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    with CatalogIndex(path) as idx:
        assert {i.id for i in idx.search()} == {"old"}  # rows preserved
        cols = {r[1] for r in idx._conn.execute("PRAGMA table_info(items)")}
        assert "place" in cols  # migration added the column
        assert "thumbnail" in cols  # later additive columns are added too
        # The migrated index labels normally.
        assert idx.bake_places(geocoder=lambda lat, lon: "Migrated Place") == 1
        assert next(iter(idx.search())).place == "Migrated Place"

    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


# -- baked thumbnails (bake_thumbnails / get_thumbnail) ----------------------


def _counting_renderer(png=b"\x89PNG\r\n\x1a\nfake"):
    """A deterministic thumbnail renderer that records which items it rendered.

    Returns ``(fn, ids)`` where ``fn(item)`` yields fixed PNG bytes and appends
    the item's id to ``ids`` -- so a test can assert both the stored bytes and
    how many scenes were actually rendered (idempotence).
    """
    ids: list[str] = []

    def fn(item):
        ids.append(item.id)
        return png

    return fn, ids


def test_bake_thumbnails_stores_and_returns_png(tmp_path):
    with _index(tmp_path) as idx:
        render, rendered = _counting_renderer()
        assert idx.bake_thumbnails(render) == 3
        assert sorted(rendered) == ["a", "b", "c"]  # every GEC item rendered
        assert idx.get_thumbnail("a") == b"\x89PNG\r\n\x1a\nfake"
        # Unknown id (or unbaked) is a clean None, not an error.
        assert idx.get_thumbnail("nope") is None


def test_bake_thumbnails_records_what_it_rendered(tmp_path):
    """The index used to store a preview's bytes and nothing about how they were
    made, so every consumer had to assume the default bake."""
    with _index(tmp_path) as idx:
        idx.bake_thumbnails(lambda item: b"png", asset="gec", max_size=128)
        assert idx.get_preview("a") == BakedPreview(png=b"png", asset="GEC", max_size=128)
        # The bytes-only reader is unchanged for the callers that only want pixels.
        assert idx.get_thumbnail("a") == b"png"
        # An unbaked or unknown scene is a clean None on both.
        assert idx.get_preview("nope") is None


def test_bake_thumbnails_is_idempotent(tmp_path):
    with _index(tmp_path) as idx:
        render, rendered = _counting_renderer()
        assert idx.bake_thumbnails(render) == 3
        rendered.clear()
        # A second run has nothing new to do -- no item is re-rendered.
        assert idx.bake_thumbnails(render) == 0
        assert rendered == []


def test_bake_thumbnails_limit_batches(tmp_path):
    with _index(tmp_path) as idx:
        render, _ = _counting_renderer()
        assert idx.bake_thumbnails(render, limit=2) == 2
        assert idx.stats()["thumbnailed"] == 2
        # The remaining item is baked on the next run.
        assert idx.bake_thumbnails(render) == 1
        assert idx.stats()["thumbnailed"] == 3


def test_bake_thumbnails_skips_unrenderable(tmp_path):
    """A renderer returning None leaves the item unbaked, to retry next run."""
    with _index(tmp_path) as idx:
        assert idx.bake_thumbnails(lambda item: None) == 0
        assert idx.stats()["thumbnailed"] == 0
        # A later successful run still finds it (it was never marked).
        assert idx.bake_thumbnails(lambda item: b"png") == 3


def test_bake_thumbnails_only_items_with_asset(tmp_path):
    """Only acquisitions carrying the requested asset are considered."""
    with _index(tmp_path) as idx:
        # No item has a CSI asset, so nothing is baked for it.
        assert idx.bake_thumbnails(lambda item: b"png", asset="CSI") == 0


def test_stats_reports_thumbnailed_count(tmp_path):
    with _index(tmp_path) as idx:
        assert idx.stats()["thumbnailed"] == 0
        idx.bake_thumbnails(lambda item: b"png", limit=2)
        assert idx.stats()["thumbnailed"] == 2


def test_thumbnail_column_migration_from_v2(tmp_path):
    """A version-2 index (place but no thumbnail) is migrated in place.

    The v2->v3 additive migration adds the ``thumbnail`` column, stamps the
    current version, and preserves every row -- and a bake then works against it.
    """
    import sqlite3

    from umbra_py.index import _SCHEMA_VERSION

    # The version-2 schema: today's `items` table minus the `thumbnail` column.
    v2_schema = """
    CREATE TABLE items (
        href TEXT PRIMARY KEY, id TEXT NOT NULL, task TEXT, datetime TEXT,
        acq_date TEXT, min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
        doc TEXT NOT NULL, place TEXT
    );
    CREATE TABLE item_assets (href TEXT NOT NULL, asset TEXT NOT NULL,
        PRIMARY KEY (href, asset));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(v2_schema)
    conn.execute(
        "INSERT INTO items (href, id, doc) VALUES (?, ?, ?)",
        ("h", "old", '{"id": "old", "assets": {}}'),
    )
    conn.execute("INSERT INTO item_assets (href, asset) VALUES (?, ?)", ("h", "GEC"))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    with CatalogIndex(path) as idx:
        assert {i.id for i in idx.search()} == {"old"}  # rows preserved
        cols = {r[1] for r in idx._conn.execute("PRAGMA table_info(items)")}
        assert "thumbnail" in cols  # migration added the column
        assert idx.bake_thumbnails(lambda item: b"png") == 1
        assert idx.get_thumbnail("old") == b"png"

    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


def test_thumbnail_provenance_migration_from_v3(tmp_path):
    """A version-3 index (thumbnails, but no record of what they are) is migrated
    in place: the columns are added, the baked pixels are preserved, and a
    preview from before the record reads as "unknown" rather than as the default.
    """
    import sqlite3

    from umbra_py.index import _SCHEMA_VERSION

    # The version-3 schema: today's `items` table minus the two v4 columns.
    v3_schema = """
    CREATE TABLE items (
        href TEXT PRIMARY KEY, id TEXT NOT NULL, task TEXT, datetime TEXT,
        acq_date TEXT, min_lon REAL, min_lat REAL, max_lon REAL, max_lat REAL,
        doc TEXT NOT NULL, place TEXT, thumbnail BLOB
    );
    CREATE TABLE item_assets (href TEXT NOT NULL, asset TEXT NOT NULL,
        PRIMARY KEY (href, asset));
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(v3_schema)
    conn.execute(
        "INSERT INTO items (href, id, doc, thumbnail) VALUES (?, ?, ?, ?)",
        ("h", "old", '{"id": "old", "assets": {}}', b"already-baked"),
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    with CatalogIndex(path) as idx:
        cols = {r[1] for r in idx._conn.execute("PRAGMA table_info(items)")}
        assert {"thumbnail_asset", "thumbnail_size"} <= cols
        assert idx.get_thumbnail("old") == b"already-baked"  # pixels preserved
        assert idx.get_preview("old") == BakedPreview(png=b"already-baked")

    version = sqlite3.connect(str(path)).execute("PRAGMA user_version").fetchone()[0]
    assert version == _SCHEMA_VERSION


def test_bake_thumbnails_newest_first_spends_a_capped_run_on_fresh_scenes(tmp_path):
    """A capped bake takes the most recent acquisitions, not catalog order.

    Default order is by ``href``, which is arbitrary with respect to time; the
    fixture's newest pass (``b``, 2024-02-10) sorts *after* the oldest (``c``,
    2023-06-01) under it, so a limit of one would leave the freshest scene
    unbaked. ``newest_first`` is what makes the cap a priority.
    """
    with _index(tmp_path) as idx:
        render, rendered = _counting_renderer()
        assert idx.bake_thumbnails(render, limit=1, newest_first=True) == 1
        assert rendered == ["b"]  # 2024-02-10, the newest pass
        rendered.clear()
        assert idx.bake_thumbnails(render, limit=1, newest_first=True) == 1
        assert rendered == ["a"]  # 2024-01-15, the next newest


def test_bake_thumbnails_newest_first_orders_undated_last(tmp_path):
    """An item with no acquisition date has no claim to being recent."""
    undated = _make_item("SiteZ", "no-date-here", "z", None, (2, 2, 3, 3))
    with _index(tmp_path, items=(_C, undated)) as idx:
        render, rendered = _counting_renderer()
        idx.bake_thumbnails(render, newest_first=True)
        assert rendered == ["c", "z"]


# -- the thumbnail sidecar (export_thumbnails / import_thumbnails) -----------


def test_export_thumbnails_round_trips_into_another_index(tmp_path):
    """Baked pixels move between indexes without re-streaming a single COG."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    with _index(src_dir) as src:
        src.bake_thumbnails(lambda item: b"png-" + item.id.encode())
        assert src.export_thumbnails(tmp_path / "catalog.thumbs.db") == 3

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    with _index(dst_dir) as dst:
        assert dst.stats()["thumbnailed"] == 0
        assert dst.import_thumbnails(tmp_path / "catalog.thumbs.db") == 3
        assert dst.get_thumbnail("a") == b"png-a"
        assert dst.stats()["thumbnailed"] == 3


def test_import_thumbnails_keeps_local_bakes_unless_overwriting(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    with _index(src_dir) as src:
        src.bake_thumbnails(lambda item: b"published")
        src.export_thumbnails(tmp_path / "catalog.thumbs.db")

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    with _index(dst_dir) as dst:
        dst.bake_thumbnails(lambda item: b"local" if item.id == "a" else None)
        # The locally baked one is left alone; only the two gaps are filled.
        assert dst.import_thumbnails(tmp_path / "catalog.thumbs.db") == 2
        assert dst.get_thumbnail("a") == b"local"
        # ...until asked to replace it (e.g. after a re-bake at another size).
        assert dst.import_thumbnails(tmp_path / "catalog.thumbs.db", overwrite=True) == 3
        assert dst.get_thumbnail("a") == b"published"


def test_import_thumbnails_ignores_rows_the_index_does_not_hold(tmp_path):
    """A sidecar from a newer crawl is not an error -- the extras are skipped."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    with _index(src_dir) as src:
        src.bake_thumbnails(lambda item: b"png")
        src.export_thumbnails(tmp_path / "catalog.thumbs.db")

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    with _index(dst_dir, items=(_A,)) as dst:
        assert dst.import_thumbnails(tmp_path / "catalog.thumbs.db") == 1
        assert dst.stats()["thumbnailed"] == 1


def test_export_thumbnails_is_an_upsert(tmp_path):
    """Exporting twice into the same sidecar refreshes rather than duplicates."""
    import sqlite3

    out = tmp_path / "catalog.thumbs.db"
    with _index(tmp_path) as idx:
        idx.bake_thumbnails(lambda item: b"first")
        assert idx.export_thumbnails(out) == 3
        idx.bake_thumbnails(lambda item: b"second", limit=1)  # already baked: no-op
        assert idx.export_thumbnails(out) == 3
    conn = sqlite3.connect(str(out))
    counts = conn.execute("SELECT COUNT(*), COUNT(DISTINCT href) FROM thumbnails").fetchone()
    assert counts == (3, 3)


def test_export_thumbnails_carries_what_each_preview_is(tmp_path):
    """The sidecar moves the bake's record, not only its pixels."""
    import sqlite3

    out = tmp_path / "catalog.thumbs.db"
    with _index(tmp_path) as idx:
        idx.bake_thumbnails(lambda item: b"png", asset="GEC", max_size=128)
        assert idx.export_thumbnails(out) == 3
    rows = sqlite3.connect(str(out)).execute("SELECT asset, size FROM thumbnails").fetchall()
    assert set(rows) == {("GEC", 128)}


def test_import_thumbnails_prefers_the_larger_bake_of_the_same_product(tmp_path):
    """A merge used to keep whichever preview arrived first, which made a scene's
    preview resolution a fact about the order two commands were run in. With both
    sides recorded, the bigger bake of the same product wins."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    sidecar = tmp_path / "catalog.thumbs.db"
    with _index(src_dir) as src:
        src.bake_thumbnails(lambda item: b"big", asset="GEC", max_size=512)
        src.export_thumbnails(sidecar)

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    with _index(dst_dir) as dst:
        dst.bake_thumbnails(lambda item: b"small", asset="GEC", max_size=128)
        assert dst.import_thumbnails(sidecar) == 3
        assert dst.get_thumbnail("a") == b"big"
        preview = dst.get_preview("a")
        assert (preview.asset, preview.max_size) == ("GEC", 512)
        # ...and the reverse merge leaves the bigger one alone.
        assert dst.export_thumbnails(sidecar) == 3  # the sidecar now holds 512 px
        assert dst.import_thumbnails(sidecar) == 0


def test_import_thumbnails_keeps_a_local_bake_it_cannot_compare(tmp_path):
    """A smaller incoming preview, one of another product, and one from a sidecar
    that predates the record are all "not obviously better" -- so the local bake
    stays, exactly as it did before the columns existed."""
    # One acquisition, carrying both products so either can be baked from it.
    both = _make_item(
        "SiteA",
        "2024-01-15-10-00-00_UMBRA-04",
        "a",
        "2024-01-15T10:00:00Z",
        (0, 0, 1, 1),
        products=("GEC", "CSI"),
    )

    def sidecar(name, **bake):
        src_dir = tmp_path / f"src-{name}"
        src_dir.mkdir()
        path = tmp_path / f"{name}.thumbs.db"
        with _index(src_dir, items=(both,)) as src:
            src.bake_thumbnails(lambda item: b"incoming", **bake)
            src.export_thumbnails(path)
        return path

    smaller = sidecar("smaller", asset="GEC", max_size=128)
    other = sidecar("other", asset="CSI", max_size=512)

    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    with _index(dst_dir, items=(both,)) as dst:
        dst.bake_thumbnails(lambda item: b"local", asset="GEC", max_size=256)
        assert dst.import_thumbnails(smaller) == 0
        assert dst.import_thumbnails(other) == 0
        assert dst.get_thumbnail("a") == b"local"


def test_thumbnail_sidecar_without_the_record_still_round_trips(tmp_path):
    """The published catalog.thumbs.db predates the asset/size columns: it must
    still import (as "unknown", so it fills gaps and clobbers nothing), and an
    export into it must widen the file rather than fail on the new columns."""
    import sqlite3

    legacy = tmp_path / "legacy.thumbs.db"
    conn = sqlite3.connect(str(legacy))
    conn.executescript(
        "CREATE TABLE thumbnails (href TEXT PRIMARY KEY, id TEXT NOT NULL, png BLOB NOT NULL);"
    )
    conn.execute("INSERT INTO thumbnails (href, id, png) VALUES (?, ?, ?)", (_A.href, "a", b"old"))
    conn.commit()
    conn.close()

    with _index(tmp_path) as idx:
        assert idx.import_thumbnails(legacy) == 1
        assert idx.get_thumbnail("a") == b"old"
        # Nothing is claimed about a preview the sidecar said nothing about.
        assert idx.get_preview("a") == BakedPreview(png=b"old", asset=None, max_size=None)
        # A local bake of the other two, then an export back into the old file.
        idx.bake_thumbnails(lambda item: b"png", asset="GEC", max_size=256)
        assert idx.export_thumbnails(legacy) == 3

    cols = {r[1] for r in sqlite3.connect(str(legacy)).execute("PRAGMA table_info(thumbnails)")}
    assert {"asset", "size"} <= cols


def test_import_thumbnails_rejects_a_file_that_is_not_a_sidecar(tmp_path):
    from umbra_py.exceptions import IndexSchemaError

    bogus = tmp_path / "not-a-sidecar.db"
    bogus.write_bytes(b"definitely not sqlite")
    with _index(tmp_path) as idx:
        try:
            idx.import_thumbnails(bogus)
        except IndexSchemaError as exc:
            assert "thumbnail sidecar" in str(exc)
        else:  # pragma: no cover - the assertion below reports the failure
            raise AssertionError("expected IndexSchemaError")

        try:
            idx.import_thumbnails(tmp_path / "absent.db")
        except FileNotFoundError as exc:
            assert "absent.db" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected FileNotFoundError")


def test_default_thumbs_path_sits_beside_the_index(tmp_path):
    from umbra_py.index import default_thumbs_path

    assert default_thumbs_path(tmp_path / "catalog.db") == tmp_path / "catalog.thumbs.db"
    assert default_thumbs_path().name == "catalog.thumbs.db"


def test_fetch_prebuilt_thumbnails_downloads_the_release_asset(tmp_path, monkeypatch):
    """The fetch helper targets the published sidecar and overwrites in place."""
    from umbra_py import index as index_mod
    from umbra_py.constants import CATALOG_INDEX_THUMBS_URL

    seen = {}

    def fake_download(url, dest, **kw):
        seen["url"] = url
        seen["overwrite"] = kw.get("overwrite")
        Path(dest).write_bytes(b"sidecar")
        return dest

    monkeypatch.setattr("umbra_py.download.download_url", fake_download)
    out = index_mod.fetch_prebuilt_thumbnails(tmp_path / "sub" / "catalog.thumbs.db")
    assert out.read_bytes() == b"sidecar"
    assert seen == {"url": CATALOG_INDEX_THUMBS_URL, "overwrite": True}


def test_cli_index_thumbnail_sidecar_round_trip(tmp_path, monkeypatch):
    """`export-thumbnails` then `fetch-thumbnails --from` moves the bake."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "src.db"
    with CatalogIndex(src) as idx:
        idx.add(_A)
        idx.add(_B)
    monkeypatch.setattr("umbra_py.viz._thumbnail_png", lambda item, **kw: b"png")
    runner = CliRunner()
    assert runner.invoke(cli_mod.cli, ["index", "bake-thumbnails", "--db", str(src)]).exit_code == 0

    sidecar = tmp_path / "catalog.thumbs.db"
    out = runner.invoke(
        cli_mod.cli, ["index", "export-thumbnails", "--db", str(src), "--out", str(sidecar)]
    )
    assert out.exit_code == 0, out.output
    assert "Exported 2 thumbnail(s)" in out.output

    dst = tmp_path / "dst.db"
    with CatalogIndex(dst) as idx:
        idx.add(_A)
        idx.add(_B)
    merged = runner.invoke(
        cli_mod.cli, ["index", "fetch-thumbnails", "--db", str(dst), "--from", str(sidecar)]
    )
    assert merged.exit_code == 0, merged.output
    assert "Merged 2 thumbnail(s)" in merged.output
    assert "2 of 2" in merged.output


def test_cli_index_export_thumbnails_defaults_beside_the_index(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)
    monkeypatch.setattr("umbra_py.viz._thumbnail_png", lambda item, **kw: b"png")
    runner = CliRunner()
    runner.invoke(cli_mod.cli, ["index", "bake-thumbnails", "--db", str(db)])
    result = runner.invoke(cli_mod.cli, ["index", "export-thumbnails", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "catalog.thumbs.db").exists()


def test_cli_index_fetch_thumbnails_downloads_when_no_source_given(tmp_path, monkeypatch):
    """Without --from, the CLI pulls the published sidecar, then merges it."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "src.db"
    with CatalogIndex(src) as idx:
        idx.add(_A)
        idx.bake_thumbnails(lambda item: b"published")
        idx.export_thumbnails(tmp_path / "release.thumbs.db")

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)

    def fake_fetch(dest, *, url=None, progress=None):
        Path(dest).write_bytes((tmp_path / "release.thumbs.db").read_bytes())
        return Path(dest)

    monkeypatch.setattr("umbra_py.cli.indexes.fetch_prebuilt_thumbnails", fake_fetch)
    result = CliRunner().invoke(cli_mod.cli, ["index", "fetch-thumbnails", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Merged 1 thumbnail(s)" in result.output
    with CatalogIndex(db) as idx:
        assert idx.get_thumbnail("a") == b"published"


def test_cli_index_fetch_thumbnails_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["index", "fetch-thumbnails", "--db", str(tmp_path / "missing.db")]
    )
    assert result.exit_code != 0
    assert "No index at" in result.output


def test_cli_index_bake_thumbnails(tmp_path, monkeypatch):
    """`umbra index bake-thumbnails` bakes via an injectable renderer path."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)
        idx.add(_B)

    # Stand in for the network/viz renderer so the CLI path stays offline.
    monkeypatch.setattr("umbra_py.viz._thumbnail_png", lambda item, **kw: b"png")

    result = CliRunner().invoke(cli_mod.cli, ["index", "bake-thumbnails", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Baked 2 new thumbnail(s)" in result.output
    assert "2 of 2" in result.output

    info = CliRunner().invoke(cli_mod.cli, ["index", "info", "--db", str(db)])
    assert "thumbs: 2 of 2 baked" in info.output


def test_cli_index_bake_thumbnails_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["index", "bake-thumbnails", "--db", str(tmp_path / "missing.db")]
    )
    assert result.exit_code != 0
    assert "No index at" in result.output


def test_cli_index_bake(tmp_path, monkeypatch):
    """`umbra index bake` labels the index and reports coverage."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)
        idx.add(_B)

    # Stand in for the network reverse-geocoder so the CLI path stays offline.
    monkeypatch.setattr("umbra_py.viz._reverse_geocode", lambda lat, lon, **kw: "Testville")

    result = CliRunner().invoke(cli_mod.cli, ["index", "bake", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Baked 2 new place label(s)" in result.output
    assert "2 of 2" in result.output

    info = CliRunner().invoke(cli_mod.cli, ["index", "info", "--db", str(db)])
    assert "places: 2 of 2 labelled" in info.output


def test_cli_index_bake_by_site(tmp_path, monkeypatch):
    """`umbra index bake --by-site` makes one lookup for a site's passes."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        for it in _site_passes("SiteRepeat", [(0, 0, 1, 1), (0.01, 0.01, 1.01, 1.01)], "r"):
            idx.add(it)

    calls: list[tuple[float, float]] = []

    def geocode(lat, lon, **kw):
        calls.append((lat, lon))
        return "Testville"

    monkeypatch.setattr("umbra_py.viz._reverse_geocode", geocode)

    result = CliRunner().invoke(cli_mod.cli, ["index", "bake", "--db", str(db), "--by-site"])
    assert result.exit_code == 0, result.output
    assert "Baked 2 new place label(s)" in result.output
    assert len(calls) == 1


def test_cli_index_bake_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["index", "bake", "--db", str(tmp_path / "missing.db")]
    )
    assert result.exit_code != 0
    assert "No index at" in result.output


# -- concurrent, multi-process access (WAL + busy timeout) --------------------


def test_index_connection_uses_wal_and_busy_timeout(tmp_path):
    """The index tunes its connection for shared, concurrent access.

    WAL journal mode lets a reader (a running ``umbra serve``) proceed while a
    writer (``umbra index update``) holds a transaction, and the busy timeout
    makes a contended access wait rather than raise ``database is locked`` at
    once. Both matter now that the published ``catalog.db`` snapshot is read by
    several processes while a CLI writer refreshes it.
    """
    with CatalogIndex(tmp_path / "catalog.db") as idx:
        mode = idx._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        timeout = idx._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000


def test_wal_journal_mode_persists_across_reopen(tmp_path):
    """WAL is a persistent property of the file, so a reopened index keeps it."""
    path = tmp_path / "catalog.db"
    CatalogIndex(path).close()
    with CatalogIndex(path) as idx:
        assert idx._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_second_connection_reads_during_open_write_transaction(tmp_path):
    """A reader on a second connection is not blocked by an in-flight writer.

    Under WAL the reader sees the last committed snapshot (three items) even
    while another connection holds an uncommitted insert -- the read-heavy
    shared-snapshot workload the published ``catalog.db`` invites, where a live
    ``umbra serve`` must keep answering while an ``umbra index update`` writes.
    """
    path = tmp_path / "catalog.db"
    writer = CatalogIndex(path)
    for it in (_A, _B, _C):
        writer.add(it)
    writer.commit()

    # A second connection opened while no write is held (its schema-ensure
    # write commits immediately).
    reader = CatalogIndex(path)

    # The writer now holds an *uncommitted* insert -- an open write transaction.
    writer.add(
        _make_item(
            "SiteC",
            "2024-03-01-00-00-00_UMBRA-04",
            "d",
            "2024-03-01T00:00:00Z",
            (20, 20, 21, 21),
        )
    )

    # The reader still sees the committed snapshot with no "database is locked".
    got = list(reader.search(limit=None))
    assert len(got) == 3

    writer.commit()
    reader.close()
    writer.close()


# --------------------------------------------------------------------------- #
# rank_sites: whole-archive, index-native repeat-imaged-site ranking
# --------------------------------------------------------------------------- #
def _site_item(task, day, *, bbox=(0.0, 0.0, 1.0, 1.0), pols=None, products=("GEC",)):
    """One dated pass of ``task`` on 2024-01-``day`` with a realistic href, so the
    index derives its task and acquisition date. Optional polarizations/footprint
    let the exact-filter (Python-side) ranking path be exercised."""
    acq = f"2024-01-{day:02d}-00-00-00_UMBRA-04"
    it = _make_item(task, acq, f"{task}-{day}", f"2024-01-{day:02d}T00:00:00Z", bbox, products)
    if pols is not None:
        it.properties["sar:polarizations"] = pols
    return it


def _rank_sites_pool_baseline(idx, **filters):
    """The uncapped-pool answer rank_sites must reproduce: rank whatever an
    unlimited search yields, exactly as the live path would."""
    from umbra_py.coverage import rank_site_coverage

    # Ranking-layer arguments (not search filters) are forwarded to the ranker;
    # everything else scopes the pool the search returns.
    top = filters.pop("top", 20)
    min_passes = filters.pop("min_passes", 2)
    rank_by = filters.pop("rank_by", "passes")
    active_since = filters.pop("active_since", None)
    active_before = filters.pop("active_before", None)
    first_since = filters.pop("first_since", None)
    first_before = filters.pop("first_before", None)
    max_revisit_days = filters.pop("max_revisit_days", None)
    median_revisit_days = filters.pop("median_revisit_days", None)
    min_span_days = filters.pop("min_span_days", None)
    max_span_days = filters.pop("max_span_days", None)
    pool = list(idx.search(limit=None, **filters))
    return rank_site_coverage(
        pool,
        top=top,
        min_passes=min_passes,
        rank_by=rank_by,
        active_since=active_since,
        active_before=active_before,
        first_since=first_since,
        first_before=first_before,
        max_revisit_days=max_revisit_days,
        median_revisit_days=median_revisit_days,
        min_span_days=min_span_days,
        max_span_days=max_span_days,
    )


def test_rank_sites_orders_by_depth_across_whole_index(tmp_path):
    pool = [
        *[_site_item("Deep", d) for d in (1, 2, 3, 4)],
        *[_site_item("Mid", d) for d in (5, 6, 7)],
        *[_site_item("Shallow", d) for d in (8, 9)],
        _site_item("Lonely", 10),  # single pass -- below min_passes
    ]
    with _index(tmp_path, pool) as idx:
        ranked = idx.rank_sites()
    assert [(s.task, s.passes) for s in ranked] == [("Deep", 4), ("Mid", 3), ("Shallow", 2)]


def test_rank_sites_recency_ranking_is_whole_archive(tmp_path):
    """The recency ranking measures a site's newest pass over the whole index, so a
    shallow but recently-imaged site is promoted past a deeper but dormant one at
    top=1 -- the raw-count LIMIT is dropped, exactly as the comparable ranking is."""
    pool = [
        *[_site_item("Deep", d) for d in (1, 2, 3, 4, 5)],  # deepest, oldest (newest 5)
        *[_site_item("Recent", d) for d in (20, 21)],  # shallow, newest 21
    ]
    with _index(tmp_path, pool) as idx:
        assert idx.rank_sites(top=1)[0].task == "Deep"
        assert idx.rank_sites(top=1, rank_by="recency")[0].task == "Recent"
        # Deep spans days 1-5 (baseline 4); Recent spans 20-21 (baseline 1).
        assert [s.task for s in idx.rank_sites(rank_by="span")] == ["Deep", "Recent"]


def test_rank_sites_matches_uncapped_pool_for_sql_filters(tmp_path):
    """The cheap GROUP-BY path is exactly the uncapped-pool ranking, for every
    SQL-expressible filter (date / bbox / area / product)."""
    pool = [
        *[_site_item("Alpha", d, products=("GEC", "SICD")) for d in (1, 5, 9)],
        *[_site_item("Beta", d, bbox=(10.0, 10.0, 11.0, 11.0)) for d in (2, 6)],
        *[_site_item("Gamma", d) for d in (3, 4)],
    ]
    with _index(tmp_path, pool) as idx:
        for filters in (
            {},
            {"min_passes": 3},
            {"top": 1},
            {"start": "2024-01-04"},
            {"end": "2024-01-05"},
            {"bbox": (9.5, 9.5, 11.5, 11.5)},
            {"area": "alph"},
            {"area": "gama", "fuzzy": True},
            {"product_types": ["SICD"]},
            # active_since gates each group on its newest pass in the same HAVING
            # clause; it must equal the pool ranker's whole-site recency gate.
            {"active_since": "2024-01-04"},
            {"active_since": "2024-01-06"},
            {"active_since": "2024-01-10"},
            # active_before is the twin MAX(acq_date) <= ? clause; a bare month
            # snaps to its last day both in SQL and the pool path, and the two
            # together bound the newest pass to a window.
            {"active_before": "2024-01-04"},
            {"active_before": "2024-01-05"},
            {"active_before": "2024-01"},
            {"active_since": "2024-01-02", "active_before": "2024-01-08"},
            # first_since / first_before gate each group on its *earliest* pass in the
            # twin MIN(acq_date) >= ? / <= ? clauses; they must equal the pool ranker's
            # whole-site onset gate. Alpha first=1, Beta first=2, Gamma first=3.
            {"first_since": "2024-01-02"},  # Beta, Gamma (Alpha's onset is earlier)
            {"first_since": "2024-01-03"},  # Gamma only
            {"first_before": "2024-01-01"},  # Alpha only
            {"first_before": "2024-01-02"},  # Alpha, Beta
            {"first_before": "2024-01"},  # bare month snaps to last day: all three
            {"first_since": "2024-01-02", "first_before": "2024-01-03"},  # Beta, Gamma
            {"first_since": "2024-01-02", "rank_by": "comparable"},
            # max_revisit is not a SQL aggregate, so the index path applies it in
            # Python on the same items the pool ranker does -- it must stay identical.
            # Alpha/Beta worst gap 4, Gamma worst gap 1.
            {"max_revisit_days": 1},  # Gamma only
            {"max_revisit_days": 4},  # all three (boundary-inclusive)
            # With top=1 the cadence filter drops the SQL LIMIT: the raw-deepest site
            # (Alpha) fails the bound and Gamma is promoted from outside the top.
            {"max_revisit_days": 3, "top": 1},
            {"max_revisit_days": 4, "rank_by": "comparable"},
            # median_revisit is the typical-cadence twin -- also not a SQL aggregate,
            # so it too is applied in Python and must stay identical to the pool ranker.
            # Alpha/Beta median gap 4, Gamma median gap 1.
            {"median_revisit_days": 1},  # Gamma only
            {"median_revisit_days": 4},  # all three (boundary-inclusive)
            # With top=1 the median filter drops the SQL LIMIT: Alpha fails the bound
            # and Gamma is promoted from outside the top.
            {"median_revisit_days": 3, "top": 1},
            {"median_revisit_days": 4, "rank_by": "comparable"},
            # min_span is not a SQL aggregate either (the comparable-subset span is
            # not a column), so it too is applied in Python and must stay identical to
            # the pool ranker. Alpha span 8 (days 1,5,9), Beta span 4, Gamma span 1.
            {"min_span_days": 1},  # all three (boundary-inclusive for Gamma)
            {"min_span_days": 5},  # Alpha only
            {"min_span_days": 8},  # Alpha only (boundary-inclusive)
            {"min_span_days": 5, "rank_by": "comparable"},
            # max_span is the upper twin -- the complement selection, applied in Python
            # the same way, so it too must stay identical to the pool ranker.
            {"max_span_days": 4},  # Beta (span 4) and Gamma (span 1)
            {"max_span_days": 1},  # Gamma only (boundary-inclusive)
            # With top=1 the ceiling drops the SQL LIMIT: the raw-deepest Alpha fails
            # and a shorter-baseline site is promoted from outside the top.
            {"max_span_days": 4, "top": 1},
            {"max_span_days": 8, "rank_by": "comparable"},
            # Floor and ceiling together bound the baseline to a window.
            {"min_span_days": 2, "max_span_days": 5},  # Beta only (span 4)
            # The temporal rankings order by MAX(acq_date) / the dated range rather
            # than COUNT, so they take the read-every-task-then-re-rank path (LIMIT
            # dropped); the index and pool orderings must stay identical, including
            # under top=1 (where the LIMIT-drop promotion happens) and combined with a
            # filter. Alpha newest 9 span 8, Beta newest 6 span 4, Gamma newest 4 span 1.
            {"rank_by": "recency"},
            {"rank_by": "span"},
            {"rank_by": "recency", "top": 1},
            {"rank_by": "span", "top": 1},
            {"rank_by": "recency", "active_since": "2024-01-05"},
            {"rank_by": "span", "min_span_days": 2},
        ):
            assert idx.rank_sites(**filters) == _rank_sites_pool_baseline(idx, **dict(filters)), (
                filters
            )


def test_rank_sites_matches_uncapped_pool_for_exact_filters(tmp_path):
    """When a polygon or acquisition-property filter is set, rank_sites takes the
    uncapped-pool branch (the SQL count can't honour those); it must still equal
    the pool ranking."""
    pool = [
        *[_site_item("Alpha", d, pols=["VV"]) for d in (1, 5, 9)],
        *[_site_item("Beta", d, pols=["HH"]) for d in (2, 6)],
    ]
    with _index(tmp_path, pool) as idx:
        polygon = {"intersects": [[(-1.0, -1.0), (-1.0, 2.0), (2.0, 2.0), (2.0, -1.0)]]}
        for filters in (
            {"polarizations": ["VV"]},
            {"max_incidence": 90.0},
            polygon,
            # A span filter set together with an exact filter still routes through the
            # uncapped-pool branch and must match the pool ranker.
            {"polarizations": ["VV"], "min_span_days": 4},
        ):
            assert idx.rank_sites(**filters) == _rank_sites_pool_baseline(idx, **dict(filters))


def test_rank_sites_min_span_filters_whole_archive_and_promotes_past_top(tmp_path):
    """min_span keeps only long-baseline sites across the whole index, and because a
    span (least of all the comparable-subset span) is applied in Python it drops the
    candidate LIMIT so a long-baseline site outside the raw top-`top` is not truncated
    before the filter runs -- the same whole-archive correction max_revisit makes, for
    the orthogonal axis (baseline, not cadence)."""
    pool = [
        *[_site_item("ShortDeep", d) for d in (10, 11, 12, 13)],  # 4 passes, span 3
        *[_site_item("LongSparse", d) for d in (1, 30)],  # 2 passes, span 29
    ]
    with _index(tmp_path, pool) as idx:
        # Without a span bound ShortDeep ranks first (more passes); top=1 returns only it.
        assert [s.task for s in idx.rank_sites(top=1)] == ["ShortDeep"]
        # The span bound drops ShortDeep and promotes LongSparse past the top-1 cap.
        long_baseline = idx.rank_sites(top=1, min_span_days=20)
    assert [(s.task, s.passes) for s in long_baseline] == [("LongSparse", 2)]


def test_rank_sites_max_span_filters_whole_archive_and_promotes_past_top(tmp_path):
    """max_span is the upper twin of min_span: it keeps only short-baseline sites and,
    because the ceiling too drops the candidate LIMIT, promotes a short-window site
    outside the raw top-`top` rather than truncating it before the filter runs."""
    pool = [
        *[_site_item("LongDeep", d) for d in (1, 10, 20, 30)],  # 4 passes, span 29
        *[_site_item("ShortSparse", d) for d in (1, 3)],  # 2 passes, span 2
    ]
    with _index(tmp_path, pool) as idx:
        # Without a ceiling LongDeep ranks first (more passes); top=1 returns only it.
        assert [s.task for s in idx.rank_sites(top=1)] == ["LongDeep"]
        # The ceiling drops LongDeep and promotes ShortSparse past the top-1 cap.
        short_baseline = idx.rank_sites(top=1, max_span_days=5)
        # Floor and ceiling together bound the baseline to a window (nothing here).
        windowed = idx.rank_sites(min_span_days=5, max_span_days=20)
    assert [(s.task, s.passes) for s in short_baseline] == [("ShortSparse", 2)]
    assert windowed == []  # neither site's span falls in [5, 20]


def test_rank_sites_active_since_filters_whole_archive_by_recency(tmp_path):
    """active_since drops a deep-but-stale site and keeps an actively-imaged one,
    across the whole index, while retaining every pass of a survivor."""
    pool = [
        *[_site_item("Fresh", d) for d in (7, 8, 9)],  # newest 2024-01-09
        *[_site_item("Stale", d) for d in (1, 2, 3)],  # newest 2024-01-03
    ]
    with _index(tmp_path, pool) as idx:
        all_sites = idx.rank_sites()
        recent = idx.rank_sites(active_since="2024-01-05")
        boundary = idx.rank_sites(active_since="2024-01-09")  # Fresh's own newest
    assert [s.task for s in all_sites] == ["Fresh", "Stale"]
    assert [(s.task, s.passes) for s in recent] == [("Fresh", 3)]  # full history kept
    assert [s.task for s in boundary] == ["Fresh"]  # on-or-after is inclusive


def test_rank_sites_active_since_on_the_exact_filter_branch(tmp_path):
    """When a polarization filter forces the uncapped-pool branch, active_since is
    forwarded to the pool ranker, so it still filters by recency."""
    pool = [
        *[_site_item("Fresh", d, pols=["VV"]) for d in (7, 8, 9)],
        *[_site_item("Stale", d, pols=["VV"]) for d in (1, 2, 3)],
    ]
    with _index(tmp_path, pool) as idx:
        recent = idx.rank_sites(polarizations=["VV"], active_since="2024-01-05")
    assert [(s.task, s.passes) for s in recent] == [("Fresh", 3)]


def test_rank_sites_active_before_filters_whole_archive_to_dormant_sites(tmp_path):
    """active_before keeps a stale site and drops an actively-imaged one, and with
    active_since bounds the site's newest pass to a window -- across the whole
    index, retaining every pass of a survivor."""
    pool = [
        *[_site_item("Fresh", d) for d in (7, 8, 9)],  # newest 2024-01-09
        *[_site_item("Stale", d) for d in (1, 2, 3)],  # newest 2024-01-03
    ]
    with _index(tmp_path, pool) as idx:
        dormant = idx.rank_sites(active_before="2024-01-05")
        boundary = idx.rank_sites(active_before="2024-01-03")  # Stale's own newest
        window = idx.rank_sites(active_since="2024-01-01", active_before="2024-01-05")
    assert [(s.task, s.passes) for s in dormant] == [("Stale", 3)]  # full history kept
    assert [s.task for s in boundary] == ["Stale"]  # on-or-before is inclusive
    assert [s.task for s in window] == ["Stale"]  # newest pass inside [since, before]


def test_rank_sites_first_since_filters_whole_archive_by_onset(tmp_path):
    """first_since keeps only newly-appeared series (earliest pass on or after the
    date) across the whole index, gating each group's MIN(acq_date) in the same
    HAVING -- the onset twin of active_since, on the earliest pass rather than the
    newest -- while retaining every pass of a survivor."""
    pool = [
        *[_site_item("New", d) for d in (10, 11, 12)],  # first 2024-01-10
        *[_site_item("Old", d) for d in (1, 2, 20)],  # first 2024-01-01 (still active later)
    ]
    with _index(tmp_path, pool) as idx:
        all_sites = idx.rank_sites()
        newly = idx.rank_sites(first_since="2024-01-05")
        boundary = idx.rank_sites(first_since="2024-01-10")  # New's own earliest
    assert {s.task for s in all_sites} == {"New", "Old"}
    assert [(s.task, s.passes) for s in newly] == [("New", 3)]  # full history kept
    assert [s.task for s in boundary] == ["New"]  # on-or-after is inclusive


def test_rank_sites_first_before_filters_whole_archive_to_established_sites(tmp_path):
    """first_before keeps long-established series (earliest pass on or before the
    date) and, with first_since, bounds the onset to a window -- across the whole
    index, orthogonally to the active_* recency gates (a site established early can
    still be imaged recently, so first_before keeps it where active_before would not)."""
    pool = [
        *[_site_item("New", d) for d in (10, 11, 12)],  # first 2024-01-10
        *[_site_item("Old", d) for d in (1, 2, 20)],  # first 2024-01-01, newest 2024-01-20
    ]
    with _index(tmp_path, pool) as idx:
        established = idx.rank_sites(first_before="2024-01-05")
        boundary = idx.rank_sites(first_before="2024-01-01")  # Old's own earliest
        window = idx.rank_sites(first_since="2024-01-05", first_before="2024-01-15")
    assert [(s.task, s.passes) for s in established] == [("Old", 3)]  # full history kept
    assert [s.task for s in boundary] == ["Old"]  # on-or-before is inclusive
    assert [s.task for s in window] == ["New"]  # onset (10th) inside [5th, 15th]


def test_rank_sites_max_revisit_filters_whole_archive_and_promotes_past_top(tmp_path):
    """max_revisit keeps only reliably-revisited sites across the whole index, and
    because the worst gap is not a SQL aggregate it drops the candidate LIMIT so a
    tightly-imaged site outside the raw top-`top` is not truncated before the filter
    runs (the whole-archive correction the pool path also makes)."""
    pool = [
        *[_site_item("DeepGappy", d) for d in (1, 2, 3, 30)],  # 4 passes, worst gap 27
        *[_site_item("Tight", d) for d in (10, 12)],  # 2 passes, worst gap 2
    ]
    with _index(tmp_path, pool) as idx:
        # Without a cadence bound DeepGappy ranks first; top=1 would return only it.
        assert [s.task for s in idx.rank_sites(top=1)] == ["DeepGappy"]
        # The cadence bound drops DeepGappy and promotes Tight past the top-1 cap.
        tight = idx.rank_sites(top=1, max_revisit_days=5)
    assert [(s.task, s.passes) for s in tight] == [("Tight", 2)]


def test_rank_sites_median_revisit_filters_whole_archive_and_promotes_past_top(tmp_path):
    """median_revisit keeps only sites usually imaged often across the whole index,
    and because the median gap is not a SQL aggregate it drops the candidate LIMIT so
    a regularly-imaged site outside the raw top-`top` is not truncated before the
    filter runs -- and it selects the complement of max_revisit (a mostly-tight series
    with one outage passes here but fails the worst-case bound)."""
    pool = [
        *[_site_item("DeepSparse", d) for d in (1, 12, 23, 28)],  # 4 passes, median 11
        *[_site_item("Bursty", d) for d in (10, 12, 14, 30)],  # 4 passes, median 2, worst 16
    ]
    with _index(tmp_path, pool) as idx:
        # Without a bound DeepSparse ties on depth but sorts first by task name.
        assert [s.task for s in idx.rank_sites(top=1)] == ["Bursty"]
        # median<=5 keeps only Bursty; drop it and DeepSparse is the sole survivor of
        # a wide-enough bound -- confirming the median gate is measured whole-archive.
        assert [s.task for s in idx.rank_sites(median_revisit_days=5)] == ["Bursty"]
        # The worst-case bound rejects Bursty (16-day outage) though its median is tight.
        assert idx.rank_sites(median_revisit_days=5, max_revisit_days=8) == []


def test_rank_sites_by_comparable_reranks_whole_archive(tmp_path):
    """rank_by='comparable' orders by analysable depth over the whole index, even
    though the analysable depth is not a COUNT the candidate SQL can order by."""
    pool = [
        # Broad: 5 raw passes, only the VV pair differenceable -> comparable 2.
        *[_site_item("Broad", d, pols=["VV"]) for d in (1, 2)],
        *[_site_item("Broad", d, pols=["HH"]) for d in (3, 4)],
        _site_item("Broad", 5, pols=["VH"]),
        # Deep: 3 raw passes, all VV -> comparable 3.
        *[_site_item("Deep", d, pols=["VV"]) for d in (6, 7, 8)],
    ]
    with _index(tmp_path, pool) as idx:
        by_passes = idx.rank_sites(rank_by="passes")
        by_comparable = idx.rank_sites(rank_by="comparable")
    assert [(s.task, s.comparable_passes) for s in by_passes] == [("Broad", 2), ("Deep", 3)]
    assert [(s.task, s.comparable_passes) for s in by_comparable] == [("Deep", 3), ("Broad", 2)]


def test_rank_sites_by_comparable_survives_the_top_cap(tmp_path):
    """A deeply-analysable site with fewer raw passes than the broad ones must
    still be the top result under comparable ranking with top=1 -- the candidate
    SQL's raw-count LIMIT is dropped for the comparable path so it is not lost."""
    pool = [
        *[_site_item("MixA", d, pols=(["VV"] if d % 2 else ["HH"])) for d in (1, 2, 3, 4, 5, 6)],
        *[_site_item("MixB", d, pols=(["VV"] if d % 2 else ["HH"])) for d in range(7, 13)],
        *[_site_item("Clean", d, pols=["VV"]) for d in (13, 14, 15, 16)],
    ]
    with _index(tmp_path, pool) as idx:
        assert idx.rank_sites(top=1, rank_by="passes")[0].comparable_passes == 3
        assert idx.rank_sites(top=1, rank_by="comparable")[0].task == "Clean"


def test_rank_sites_by_comparable_matches_the_pool_ranking(tmp_path):
    """The index comparable ranking equals the uncapped-pool comparable ranking,
    on both the SQL-candidate path and the exact-filter (Python) path."""
    pool = [
        *[_site_item("Alpha", d, pols=(["VV"] if d % 2 else ["HH"])) for d in (1, 3, 5, 7)],
        *[_site_item("Beta", d, pols=["VV"]) for d in (2, 6)],
        *[_site_item("Gamma", d, pols=["HH"]) for d in (4, 8, 9)],
    ]
    with _index(tmp_path, pool) as idx:
        from umbra_py.coverage import rank_site_coverage

        pool_ranked = rank_site_coverage(list(idx.search(limit=None)), rank_by="comparable")
        assert idx.rank_sites(rank_by="comparable") == pool_ranked


def test_rank_sites_min_passes_gates_comparable_depth(tmp_path):
    """Under rank_by='comparable', min_passes floors the analysable depth, not the
    raw count -- a site whose raw passes clear the SQL HAVING but whose comparable
    series is shallower than min_passes is dropped, matching the pool ranking."""
    pool = [
        # Mixed: 3 dated passes, each a different polarization -> comparable 1.
        _site_item("Mixed", 1, pols=["VV"]),
        _site_item("Mixed", 2, pols=["HH"]),
        _site_item("Mixed", 3, pols=["VH"]),
        # Deep: 2 passes, both VV -> comparable 2.
        *[_site_item("Deep", d, pols=["VV"]) for d in (4, 5)],
    ]
    with _index(tmp_path, pool) as idx:
        from umbra_py.coverage import rank_site_coverage

        # Raw ranking admits Mixed (3 raw passes >= 2); comparable ranking drops it.
        assert {s.task for s in idx.rank_sites(min_passes=2, rank_by="passes")} == {"Mixed", "Deep"}
        by_comparable = idx.rank_sites(min_passes=2, rank_by="comparable")
        assert [s.task for s in by_comparable] == ["Deep"]
        # Still exactly the uncapped-pool comparable ranking with the same floor.
        pool_ranked = rank_site_coverage(
            list(idx.search(limit=None)), min_passes=2, rank_by="comparable"
        )
        assert by_comparable == pool_ranked
        # Exact-filter branch (polarizations set) must agree too.
        assert idx.rank_sites(rank_by="comparable", polarizations=["VV"]) == rank_site_coverage(
            list(idx.search(limit=None, polarizations=["VV"])), rank_by="comparable"
        )


def test_rank_sites_rejects_unknown_rank_by(tmp_path):
    import pytest

    with _index(tmp_path, [_site_item("Alpha", d) for d in (1, 2)]) as idx:
        with pytest.raises(ValueError, match="rank_by must be one of"):
            idx.rank_sites(rank_by="deepest")


def test_rank_sites_top_zero_and_empty_index(tmp_path):
    with _index(tmp_path, [_site_item("Alpha", d) for d in (1, 2)]) as idx:
        assert idx.rank_sites(top=0) == []
    empty = tmp_path / "empty"
    empty.mkdir()
    with _index(empty, []) as idx:
        assert idx.rank_sites() == []
