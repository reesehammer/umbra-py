"""Offline tests for umbra watch: idempotent delta detection.

No network: the search source is a tiny in-memory fake, and the state store is
either the in-memory store or a real :class:`CatalogIndex` meta table on a
tmp-path SQLite file. ``checked_at`` is injected so results are deterministic.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from umbra_py import (
    InMemoryWatchStore,
    MetaWatchStore,
    watch,
    watch_key,
)
from umbra_py.cli import cli
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _make_item(task, acq, item_id, dt, bbox=(0, 0, 1, 1), products=("GEC",)):
    """Build an UmbraItem with a realistic sidecar href (its watch key)."""
    base = f"{_BUCKET}/sar-data/tasks/{task}/{acq}/{acq}"
    href = f"{base}.stac.v2.json"
    assets: dict[str, dict] = {}
    for p in products:
        assets[f"{acq}_{p}.tif"] = {
            "href": f"{base}_{p}.tif",
            "type": "image/tiff; application=geotiff; profile=cloud-optimized",
        }
    doc = {
        "id": item_id,
        "properties": {"datetime": dt, "sar:product_type": products[0]},
        "bbox": list(bbox),
        "geometry": None,
        "assets": assets,
    }
    return UmbraItem.from_dict(doc, href=href)


_A = _make_item("SiteA", "2024-01-15-10-00-00_UMBRA-04", "a", "2024-01-15T10:00:00Z")
_B = _make_item("SiteA", "2024-02-10-12-00-00_UMBRA-09", "b", "2024-02-10T12:00:00Z")
_C = _make_item("SiteA", "2024-03-01-00-00-00_UMBRA-04", "c", "2024-03-01T00:00:00Z")


class FakeCatalog:
    """A search source whose result set the test controls between runs."""

    def __init__(self, items):
        self.items = list(items)

    def search(self, **kwargs):
        # Honor a limit if given, ignore the rest -- the fixture data is already
        # the "matching" set for the query under test.
        limit = kwargs.get("limit")
        items = self.items if limit is None else self.items[:limit]
        yield from items


def test_first_run_reports_everything_as_baseline():
    store = InMemoryWatchStore()
    result = watch(FakeCatalog([_A, _B]), name="siteA", store=store, checked_at="2024-02-11")
    assert result.first_run is True
    assert result.new_count == 2
    assert {i.id for i in result.new_items} == {"a", "b"}
    assert result.total_seen == 2
    assert result.checked_at == "2024-02-11"


def test_second_run_reports_only_new():
    store = InMemoryWatchStore()
    catalog = FakeCatalog([_A, _B])
    watch(catalog, name="siteA", store=store)  # baseline
    catalog.items = [_A, _B, _C]  # one new pass lands
    result = watch(catalog, name="siteA", store=store)
    assert result.first_run is False
    assert result.new_count == 1
    assert result.new_items[0].id == "c"
    assert result.total_seen == 3


def test_idempotent_rerun_reports_nothing():
    store = InMemoryWatchStore()
    catalog = FakeCatalog([_A, _B, _C])
    watch(catalog, name="siteA", store=store)  # baseline
    result = watch(catalog, name="siteA", store=store)  # identical data
    assert result.new_count == 0
    assert result.first_run is False
    assert result.total_seen == 3


def test_dropped_then_reappearing_item_is_not_re_reported():
    # A previously-seen acquisition that leaves and re-enters the result set must
    # not alert again -- the union-of-keys state, not a live diff, guarantees it.
    store = InMemoryWatchStore()
    catalog = FakeCatalog([_A, _B])
    watch(catalog, name="siteA", store=store)
    catalog.items = [_A]  # _B temporarily absent
    watch(catalog, name="siteA", store=store)
    catalog.items = [_A, _B]  # _B back
    result = watch(catalog, name="siteA", store=store)
    assert result.new_count == 0


def test_reset_re_establishes_baseline():
    store = InMemoryWatchStore()
    catalog = FakeCatalog([_A, _B])
    watch(catalog, name="siteA", store=store)
    result = watch(catalog, name="siteA", store=store, reset=True)
    assert result.first_run is True
    assert result.new_count == 2


def test_distinct_names_track_independently():
    store = InMemoryWatchStore()
    watch(FakeCatalog([_A]), name="one", store=store)
    result = watch(FakeCatalog([_A, _B]), name="two", store=store)
    # "two" has never been seen, so both are new for it.
    assert result.new_count == 2


def test_item_without_href_falls_back_to_id():
    doc = {"id": "no-href", "properties": {"datetime": "2024-01-01T00:00:00Z"}, "assets": {}}
    item = UmbraItem.from_dict(doc, href=None)
    store = InMemoryWatchStore()
    result = watch(FakeCatalog([item]), name="x", store=store)
    assert result.new_count == 1
    # A second run keys off the id and does not re-report it.
    again = watch(FakeCatalog([item]), name="x", store=store)
    assert again.new_count == 0


def test_to_dict_is_machine_readable_and_carries_attribution():
    store = InMemoryWatchStore()
    result = watch(
        FakeCatalog([_A]), name="siteA", store=store, checked_at="2024-02-11", area="SiteA"
    )
    payload = result.to_dict()
    assert payload["watch"] == "siteA"
    assert payload["new_count"] == 1
    assert payload["query"] == {"area": "SiteA"}
    assert payload["new_items"][0]["id"] == "a"
    assert "Umbra" in payload["attribution"]
    # Round-trips through JSON (the CLI --json path).
    assert json.loads(json.dumps(payload))["new_count"] == 1


def test_watch_key_is_stable_and_query_specific():
    k1 = watch_key(area="Centerfield, Utah", product_types=["GEC"])
    k2 = watch_key(area="Centerfield, Utah", product_types=["GEC"])
    k3 = watch_key(area="Centerfield, Utah", product_types=["SICD"])
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("centerfield-utah-")


def test_meta_watch_store_persists_across_index_reopen(tmp_path):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        result = watch(FakeCatalog([_A, _B]), name="siteA", store=MetaWatchStore(idx))
        assert result.new_count == 2
    # Reopen a fresh index over the same file: the baseline must survive.
    catalog = FakeCatalog([_A, _B, _C])
    with CatalogIndex(db) as idx:
        result = watch(catalog, name="siteA", store=MetaWatchStore(idx))
    assert result.new_count == 1
    assert result.new_items[0].id == "c"


# -- CLI ---------------------------------------------------------------------


def _seed_index(db):
    """A local index the CLI can search with --local (SiteA has two passes)."""
    with CatalogIndex(db) as idx:
        for it in (_A, _B):
            idx.add(it)
        idx.commit()


def test_cli_watch_local_first_run_then_idempotent(tmp_path):
    db = tmp_path / "catalog.db"
    _seed_index(db)
    runner = CliRunner()
    first = runner.invoke(
        cli, ["watch", "--local", "--index-db", str(db), "--state-db", str(db), "--area", "SiteA"]
    )
    assert first.exit_code == 0
    assert "baseline" in first.output
    assert "Tracking 2 acquisition(s)" in first.output

    second = runner.invoke(
        cli, ["watch", "--local", "--index-db", str(db), "--state-db", str(db), "--area", "SiteA"]
    )
    assert second.exit_code == 0
    assert "No new acquisitions" in second.output


def test_cli_watch_json_and_exit_code(tmp_path):
    db = tmp_path / "catalog.db"
    _seed_index(db)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "watch",
            "--local",
            "--index-db",
            str(db),
            "--state-db",
            str(db),
            "--area",
            "SiteA",
            "--json",
            "--exit-code",
        ],
    )
    # New acquisitions on the first run -> exit 10 with --exit-code.
    assert result.exit_code == 10
    payload = json.loads(result.output)
    assert payload["new_count"] == 2
    assert payload["first_run"] is True
    assert {i["id"] for i in payload["new_items"]} == {"a", "b"}


def test_cli_watch_reset_rebaselines(tmp_path):
    db = tmp_path / "catalog.db"
    _seed_index(db)
    runner = CliRunner()
    runner.invoke(
        cli, ["watch", "--local", "--index-db", str(db), "--state-db", str(db), "--area", "SiteA"]
    )
    reset = runner.invoke(
        cli,
        [
            "watch",
            "--local",
            "--index-db",
            str(db),
            "--state-db",
            str(db),
            "--area",
            "SiteA",
            "--reset",
            "--json",
        ],
    )
    assert reset.exit_code == 0
    assert json.loads(reset.output)["first_run"] is True
