"""Offline tests for the local SQLite catalog index."""

from __future__ import annotations

from umbra_py.index import CatalogIndex, default_index_path
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
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", _no_live_walk)

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
    from umbra_py import viz as viz_mod

    with _index(tmp_path, items=(_A, _B)):
        pass
    db = str(tmp_path / "catalog.db")

    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", lambda *_a, **_k: "data:image/png;base64,Z")
    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", _no_live_walk)

    out = tmp_path / "gallery.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--local", "--index-db", db, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Wrote gallery of 2 acquisition(s)" in result.output
    assert "data:image/png;base64,Z" in out.read_text()


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

    monkeypatch.setattr(cli_mod.UmbraCatalog, "search", fake_search)
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
