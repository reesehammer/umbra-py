"""Offline tests for the structured ``--json`` success output on the CLI.

These cover the success-side counterpart to the machine-readable error
contract: ``umbra download --json`` (a ``{asset, path, bytes, sha256}`` array),
``umbra index info --json`` (the index summary), and the render commands'
``{output, items_used, parameters}`` manifest. Everything is driven with
``_runner()`` so stdout carries the JSON alone -- exactly the
guarantee an agent depends on -- while progress/warnings land on stderr.
"""

from __future__ import annotations

import hashlib
import json

from click.testing import CliRunner

from umbra_py import cli as cli_mod
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _runner() -> CliRunner:
    """A CliRunner that keeps stdout and stderr separate across click versions.

    click < 8.2 mixes the streams unless ``mix_stderr=False``; click >= 8.2
    removed the argument and always separates them. Either way, ``result.stdout``
    then carries the JSON alone -- the guarantee these tests assert.
    """
    try:
        return CliRunner(mix_stderr=False)  # click < 8.2
    except TypeError:
        return CliRunner()  # click >= 8.2 (streams already separate)


def _make_item(task, acq, item_id, dt, bbox, products=("GEC",)):
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


_A = _make_item("SiteA", "2024-01-15-10-00-00_UMBRA-04", "a", "2024-01-15T10:00:00Z", (0, 0, 1, 1))
_B = _make_item("SiteA", "2024-02-10-12-00-00_UMBRA-04", "b", "2024-02-10T12:00:00Z", (0, 0, 1, 1))
_C = _make_item("SiteA", "2024-03-05-09-00-00_UMBRA-04", "c", "2024-03-05T09:00:00Z", (0, 0, 1, 1))


def _index(tmp_path, items):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        for it in items:
            idx.add(it)
    return db


# --------------------------------------------------------------------------- #
# umbra download --json
# --------------------------------------------------------------------------- #


def test_download_json_emits_records(tmp_path, monkeypatch):
    item = _A
    monkeypatch.setattr(cli_mod, "get_json", lambda url: item.raw)

    body = b"sar-bytes" * 10

    def fake_download_item(it, dest, assets, overwrite, progress):
        (name,) = assets
        path = tmp_path / f"{it.id}_{name}.tif"
        path.write_bytes(body)
        return [path]

    monkeypatch.setattr(cli_mod, "download_item", fake_download_item)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        ["download", f"{_BUCKET}/x.json", "--asset", "GEC", "--dest", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.stderr
    records = json.loads(result.stdout)
    assert isinstance(records, list) and len(records) == 1
    rec = records[0]
    assert rec["asset"] == "GEC"
    assert rec["bytes"] == len(body)
    assert rec["sha256"] == hashlib.sha256(body).hexdigest()
    assert rec["path"].endswith(".tif")
    # No human progress leaked onto stdout.
    assert "Downloading" not in result.stdout


def test_download_without_json_stays_human(tmp_path, monkeypatch):
    item = _A
    monkeypatch.setattr(cli_mod, "get_json", lambda url: item.raw)

    def fake_download_item(it, dest, assets, overwrite, progress):
        (name,) = assets
        path = tmp_path / f"{it.id}_{name}.tif"
        path.write_bytes(b"x")
        return [path]

    monkeypatch.setattr(cli_mod, "download_item", fake_download_item)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        ["download", f"{_BUCKET}/x.json", "--asset", "GEC", "--dest", str(tmp_path)],
    )
    assert result.exit_code == 0, result.stderr
    assert "Downloading GEC of a" in result.stdout
    # Human output is not JSON.
    try:
        json.loads(result.stdout)
        raise AssertionError("human output should not parse as JSON")
    except json.JSONDecodeError:
        pass


# --------------------------------------------------------------------------- #
# umbra index info --json
# --------------------------------------------------------------------------- #


def test_index_info_json(tmp_path):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_A)
        idx.add(_B)
        idx.set_meta("built_at", "2026-07-01")

    runner = _runner()
    result = runner.invoke(cli_mod.cli, ["index", "info", "--db", str(db), "--json"])
    assert result.exit_code == 0, result.stderr
    info = json.loads(result.stdout)
    assert info["path"] == str(db)
    assert info["items"] == 2
    assert info["tasks"] == 1
    assert info["start"] == "2024-01-15"
    assert info["end"] == "2024-02-10"
    assert info["built_at"] == "2026-07-01"
    assert info["size_bytes"] > 0


# --------------------------------------------------------------------------- #
# render-manifest: timescan / gallery / swipe / change / map
# --------------------------------------------------------------------------- #


def test_timescan_manifest(tmp_path, monkeypatch):
    db = _index(tmp_path, (_A, _B, _C))
    out = tmp_path / "timescan.png"

    def fake_render(items, out_path, **kwargs):
        from pathlib import Path

        Path(out_path).write_bytes(b"png")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "save_timescan_composite", fake_render)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        [
            "timescan",
            "--local",
            "--index-db",
            str(db),
            "--area",
            "SiteA",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert manifest["items_used"] == ["a", "b", "c"]
    assert manifest["parameters"]["asset"] == "GEC"
    assert "Selected" not in result.stdout


def test_gallery_manifest(tmp_path, monkeypatch):
    db = _index(tmp_path, (_A, _B))
    out = tmp_path / "gallery.html"

    def fake_gallery(items, out_path, **kwargs):
        from pathlib import Path

        Path(out_path).write_text("<html></html>")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "save_gallery", fake_gallery)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        [
            "gallery",
            "--local",
            "--index-db",
            str(db),
            "--area",
            "SiteA",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert set(manifest["items_used"]) == {"a", "b"}
    assert manifest["parameters"]["products"] == ["GEC"]


def test_swipe_manifest(tmp_path, monkeypatch):
    db = _index(tmp_path, (_A, _B))
    out = tmp_path / "swipe.html"

    def fake_swipe(before, after, out_path, **kwargs):
        from pathlib import Path

        Path(out_path).write_text("<html></html>")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "save_swipe_map", fake_swipe)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        ["swipe", "--local", "--index-db", str(db), "--area", "SiteA", "--out", str(out), "--json"],
    )
    assert result.exit_code == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert manifest["items_used"] == ["a", "b"]
    assert "Comparing" not in result.stdout


def test_change_composite_manifest(tmp_path, monkeypatch):
    db = _index(tmp_path, (_A, _B, _C))
    out = tmp_path / "change.png"

    def fake_change(items, out_path, **kwargs):
        from pathlib import Path

        Path(out_path).write_bytes(b"png")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "save_change_composite", fake_change)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        [
            "change",
            "--local",
            "--index-db",
            str(db),
            "--area",
            "SiteA",
            "--out",
            str(out),
            "--frames",
            "2",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert manifest["parameters"]["mode"] == "composite"
    assert manifest["parameters"]["frames"] == 2
    assert len(manifest["items_used"]) == 2


def test_map_geojson_manifest(tmp_path, monkeypatch):
    db = _index(tmp_path, (_A, _B))
    out = tmp_path / "footprints.geojson"

    def fake_geojson(items, out_path):
        from pathlib import Path

        Path(out_path).write_text("{}")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "write_geojson", fake_geojson)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        ["map", "--local", "--index-db", str(db), "--out", str(out), "--json"],
    )
    assert result.exit_code == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert manifest["parameters"]["format"] == "geojson"
    assert set(manifest["items_used"]) == {"a", "b"}


def test_manifests_validate_against_schema(tmp_path, monkeypatch):
    """The emitted manifest matches the published schema's required shape."""
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[1] / "docs" / "schemas" / "render-manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    required = set(schema["required"])

    db = _index(tmp_path, (_A, _B, _C))
    out = tmp_path / "timescan.png"

    def fake_render(items, out_path, **kwargs):
        Path(out_path).write_bytes(b"png")
        return Path(out_path)

    monkeypatch.setattr(cli_mod, "save_timescan_composite", fake_render)

    runner = _runner()
    result = runner.invoke(
        cli_mod.cli,
        [
            "timescan",
            "--local",
            "--index-db",
            str(db),
            "--area",
            "SiteA",
            "--out",
            str(out),
            "--json",
        ],
    )
    manifest = json.loads(result.stdout)
    assert required.issubset(manifest.keys())
    assert isinstance(manifest["items_used"], list)
    assert isinstance(manifest["parameters"], dict)
