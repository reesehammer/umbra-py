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


def test_search_limit(tmp_path):
    with _index(tmp_path) as idx:
        assert len(list(idx.search(limit=1))) == 1


def test_search_max_per_task(tmp_path):
    # SiteA has two acquisitions (a, c); SiteB has one (b).
    with _index(tmp_path) as idx:
        items = list(idx.search(max_per_task=1))
    assert len(items) == 2
    assert {i.task for i in items} == {"SiteA", "SiteB"}


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


def test_cli_search_local_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    db = str(tmp_path / "missing.db")
    result = CliRunner().invoke(cli_mod.cli, ["search", "--local", "--db", db])
    assert result.exit_code != 0
    assert "No index" in result.output
