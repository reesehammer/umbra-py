"""Resolve a GEC/SICD filename or acquisition stem through the local index.

The filenames are real open-archive names (three bucket layouts). The UUIDs
in the fixture hrefs are stand-ins: CI never fetches the catalog. What the
lookup has to get right is that the stem lives in the asset href, and that
``sar-data/tasks/``, ``sar-data/task-data/``, and ``open-data/`` are different
prefixes the filename does not encode.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import click
import pytest
import responses
from click.testing import CliRunner

from umbra_py.cli import cli
from umbra_py.exceptions import CatalogError
from umbra_py.index import (
    CatalogIndex,
    is_asset_name,
    resolve_asset_name,
)
from umbra_py.mcp_server import download_asset, get_item
from umbra_py.models import UmbraItem

_BUCKET = "https://umbra-open-data-catalog.s3.amazonaws.com"

# stem, directory above the acquisition, id, products
_KHAVDA = "2025-05-17-17-43-46_UMBRA-08"
_RED_SEA = "2025-06-15-08-28-22_UMBRA-08"
_TASK_DATA = "2025-12-18-18-07-38_UMBRA-07"
_OPEN_DATA = "2026-01-02-03-22-31_UMBRA-09"
_NEAR = "2025-05-17-17-43-46_UMBRA-081"


def _item(stem: str, prefix: str, item_id: str, products: tuple[str, ...] = ("GEC",)) -> UmbraItem:
    """One indexed acquisition whose public asset hrefs use ``stem``."""
    folder = quote(prefix, safe="/")
    base = f"{_BUCKET}/{folder}/{stem}"
    href = f"{base}/{stem}.stac.v2.json"
    assets: dict[str, dict] = {}
    for product in products:
        ext = "tif" if product in ("GEC", "CSI") else "nitf"
        filename = f"{stem}_{product}.{ext}"
        assets[filename] = {
            "href": f"{base}/{filename}",
            "type": "image/tiff; application=geotiff" if ext == "tif" else "application/vnd.nitf",
        }
    doc = {
        "id": item_id,
        "properties": {"datetime": f"{stem[:10]}T00:00:00Z"},
        "bbox": [0, 0, 1, 1],
        "assets": assets,
    }
    return UmbraItem.from_dict(doc, href=href)


def _named() -> list[UmbraItem]:
    return [
        _item(
            _KHAVDA,
            "sar-data/tasks/Khavda Solar Park, India/36d6397f-0000-4000-8000-000000000001",
            "khavda-2025-05-17",
            ("GEC", "SICD"),
        ),
        _item(
            _RED_SEA,
            "sar-data/tasks/The Red Sea Project - Shura Island, Saudi Arabia/"
            "5026ca2c-0000-4000-8000-000000000004",
            "red-sea-2025-06-15",
        ),
        _item(
            _TASK_DATA,
            "sar-data/task-data/4ae61db3-0000-4000-8000-000000000002",
            "task-data-2025-12-18",
        ),
        _item(
            _OPEN_DATA,
            "open-data/1bd599bb-0000-4000-8000-000000000003",
            "open-data-2026-01-02",
        ),
        # Same instant as Khavda, next satellite number. A LIKE on the stem
        # matches this row; the boundary check must not.
        _item(
            _NEAR,
            "sar-data/tasks/Khavda Solar Park, India/36d6397f-0000-4000-8000-0000000000aa",
            "near-miss",
        ),
    ]


def _index(tmp_path, items: list[UmbraItem] | None = None) -> CatalogIndex:
    idx = CatalogIndex(tmp_path / "catalog.db")
    for item in items if items is not None else _named():
        idx.add(item)
    idx.commit()
    return idx


def test_is_asset_name_accepts_filename_stem_and_rejects_urls():
    assert is_asset_name(f"{_KHAVDA}_GEC.tif")
    assert is_asset_name(f"{_KHAVDA}_SICD.nitf")
    assert is_asset_name(_KHAVDA)
    assert is_asset_name(f"downloads/{_KHAVDA}_GEC.tif")
    assert not is_asset_name(f"{_BUCKET}/sar-data/tasks/x/{_KHAVDA}_GEC.tif")
    assert not is_asset_name(f"{_BUCKET}/sar-data/tasks/x/{_KHAVDA}_METADATA.json")
    assert is_asset_name(f"{_KHAVDA}_METADATA.json")
    assert not is_asset_name("2d1827a2-d04c-4e15-9319-3810d011540b")
    assert not is_asset_name(f"{_KHAVDA}.stac.v2.json")


def test_find_by_asset_name_covers_the_three_layouts(tmp_path):
    with _index(tmp_path) as idx:
        by_file = idx.find_by_asset_name(f"{_KHAVDA}_GEC.tif")
        by_sicd = idx.find_by_asset_name(f"{_KHAVDA}_SICD.nitf")
        by_stem = idx.find_by_asset_name(_KHAVDA)
        by_task_data = idx.find_by_asset_name(f"{_TASK_DATA}_GEC.tif")
        by_open = idx.find_by_asset_name(_OPEN_DATA)
        by_red_sea = idx.find_by_asset_name(f"somewhere/{_RED_SEA}_GEC.tif")

    assert [item.id for item in by_file] == ["khavda-2025-05-17"]
    assert [item.id for item in by_sicd] == ["khavda-2025-05-17"]
    assert [item.id for item in by_stem] == ["khavda-2025-05-17"]
    assert "sar-data/tasks/" in by_file[0].href
    assert [item.id for item in by_task_data] == ["task-data-2025-12-18"]
    assert "sar-data/task-data/" in by_task_data[0].href
    assert [item.id for item in by_open] == ["open-data-2026-01-02"]
    assert "/open-data/" in by_open[0].href
    assert [item.id for item in by_red_sea] == ["red-sea-2025-06-15"]


def test_stem_does_not_match_a_longer_satellite_number(tmp_path):
    with _index(tmp_path) as idx:
        hits = idx.find_by_asset_name(_KHAVDA)
    assert [item.id for item in hits] == ["khavda-2025-05-17"]


def test_a_property_that_mentions_the_filename_is_not_a_match(tmp_path):
    decoy = UmbraItem.from_dict(
        {
            "id": "decoy",
            "properties": {
                "umbra:task_id": f"{_KHAVDA}_GEC.tif",
                "datetime": "2025-05-17T00:00:00Z",
            },
            "bbox": [0, 0, 1, 1],
            "assets": {"other.tif": {"href": f"{_BUCKET}/sar-data/tasks/other/other.tif"}},
        },
        href=f"{_BUCKET}/sar-data/tasks/other/other.stac.v2.json",
    )
    with _index(tmp_path, [decoy]) as idx:
        assert idx.find_by_asset_name(f"{_KHAVDA}_GEC.tif") == []
        assert idx.find_by_asset_name(_KHAVDA) == []


def test_resolve_errors_when_missing_unknown_or_ambiguous(tmp_path):
    missing = tmp_path / "absent" / "catalog.db"
    with pytest.raises(CatalogError, match="No local catalog index") as missing_exc:
        resolve_asset_name(_KHAVDA, path=missing)
    assert not missing.exists()
    assert missing_exc.value.hint and "umbra index fetch" in missing_exc.value.hint

    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    with pytest.raises(CatalogError, match="No acquisition") as unknown:
        resolve_asset_name("2024-01-01-00-00-00_UMBRA-04_GEC.tif", path=db)
    assert unknown.value.hint and "STAC JSON URL" in unknown.value.hint

    copy = _item(
        _KHAVDA,
        "sar-data/task-data/36d6397f-0000-4000-8000-000000000099",
        "khavda-republish",
        ("GEC",),
    )
    with _index(tmp_path, [_named()[0], copy]):
        pass
    with pytest.raises(CatalogError, match="2 acquisitions match") as ambiguous:
        resolve_asset_name(f"{_KHAVDA}_GEC.tif", path=db)
    text = str(ambiguous.value)
    assert "sar-data/tasks/" in text
    assert "sar-data/task-data/" in text
    assert ambiguous.value.hint == "Pass one of these STAC JSON URLs."


def test_filename_lookup_keeps_datetime_suspect(tmp_path):
    """A flagged row stays flagged when it is opened by filename."""
    db = tmp_path / "catalog.db"
    with _index(tmp_path) as idx:
        idx._conn.execute(
            "UPDATE items SET datetime_suspect = 1 WHERE id = ?",
            ("khavda-2025-05-17",),
        )
        idx.commit()
    item = resolve_asset_name(f"{_KHAVDA}_GEC.tif", path=db)
    assert item.datetime_suspect is True
    assert item.id == "khavda-2025-05-17"


def test_resolve_returns_the_one_indexed_item(tmp_path):
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    item = resolve_asset_name(_OPEN_DATA, path=db)
    assert item.id == "open-data-2026-01-02"
    assert item.asset_href("GEC").endswith(f"{_OPEN_DATA}_GEC.tif")


def _runner() -> CliRunner:
    return CliRunner()


def test_info_and_download_resolve_a_filename(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))

    info = _runner().invoke(cli, ["info", f"{_TASK_DATA}_GEC.tif"])
    assert info.exit_code == 0, info.output
    assert "task-data-2025-12-18" in info.output

    stem = _runner().invoke(cli, ["info", _OPEN_DATA, "--json"])
    assert stem.exit_code == 0, stem.output
    card = json.loads(stem.output)
    assert card["id"] == "open-data-2026-01-02"

    captured: dict = {}

    def fake_download(item, dest, assets, overwrite, progress):
        captured["id"] = item.id
        captured["assets"] = list(assets)
        path = tmp_path / "out.tif"
        path.write_bytes(b"gec")
        return [path]

    monkeypatch.setattr("umbra_py.cli.scenes.download_item", fake_download)
    downloaded = _runner().invoke(
        cli,
        ["download", f"{_KHAVDA}_SICD.nitf", "--asset", "SICD", "--dest", str(tmp_path)],
    )
    assert downloaded.exit_code == 0, downloaded.output
    assert captured == {"id": "khavda-2025-05-17", "assets": ["SICD"]}


def test_info_url_still_fetches_json(monkeypatch):
    seen: list[str] = []

    def fake_get_json(url: str) -> dict:
        seen.append(url)
        return {
            "id": "from-url",
            "properties": {"datetime": "2024-01-01T00:00:00Z"},
            "bbox": [0, 0, 1, 1],
            "assets": {},
        }

    monkeypatch.setattr("umbra_py.cli._shared.get_json", fake_get_json)
    url = f"{_BUCKET}/sar-data/tasks/x/{_KHAVDA}/{_KHAVDA}.stac.v2.json"
    result = _runner().invoke(cli, ["info", url])
    assert result.exit_code == 0, result.output
    assert seen == [url]
    assert "from-url" in result.output


def test_info_filename_with_token_uses_the_index_not_canopy(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))

    def boom(self, item_id):
        raise AssertionError(f"canopy lookup {item_id}")

    monkeypatch.setattr("umbra_py.cli.discover.UmbraCatalog.get_item", boom)
    result = _runner().invoke(cli, ["info", _KHAVDA, "--token", "secret"])
    assert result.exit_code == 0, result.output
    assert "khavda-2025-05-17" in result.output


def test_cli_reports_missing_unknown_and_ambiguous_names(tmp_path, monkeypatch):
    missing = _runner().invoke(
        cli, ["info", _KHAVDA], env={"UMBRA_INDEX_DB": str(tmp_path / "nope.db")}
    )
    assert missing.exit_code != 0
    assert "No local catalog index" in missing.output
    assert "umbra index fetch" in missing.output
    assert not (tmp_path / "nope.db").exists()

    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))
    unknown = _runner().invoke(cli, ["download", "2024-01-01-00-00-00_UMBRA-04"])
    assert unknown.exit_code != 0
    assert "No acquisition" in unknown.output

    copy = _item(
        _KHAVDA,
        "sar-data/task-data/36d6397f-0000-4000-8000-000000000099",
        "khavda-republish",
    )
    with CatalogIndex(db) as idx:
        idx.add(copy)
        idx.commit()
    ambiguous = _runner().invoke(cli, ["info", f"{_KHAVDA}_GEC.tif"])
    assert ambiguous.exit_code != 0
    assert "sar-data/tasks/" in ambiguous.output
    assert "sar-data/task-data/" in ambiguous.output
    assert "Pass one of these STAC JSON URLs." in ambiguous.output


def test_download_flag_passes_v2_keys_and_filenames(tmp_path, monkeypatch):
    """``--asset`` is not a product-type Choice: ``gec`` stays ``gec``, and ``gec_2`` is legal."""
    from umbra_py.cli.scenes import download as download_cmd

    param = next(p for p in download_cmd.params if p.name == "assets")
    assert not isinstance(param.type, click.Choice)
    assert param.multiple is True

    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))
    seen: list[str] = []

    def fake_download(item, dest, assets, overwrite, progress):
        seen.extend(assets)
        path = tmp_path / "out.tif"
        path.write_bytes(b"x")
        return [path]

    monkeypatch.setattr("umbra_py.cli.scenes.download_item", fake_download)
    filename = f"{_KHAVDA}_GEC.tif"
    result = _runner().invoke(
        cli,
        [
            "download",
            _KHAVDA,
            "--asset",
            "gec",
            "--asset",
            "gec_2",
            "--asset",
            filename,
            "--dest",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["gec", "gec_2", filename]
    help_text = _runner().invoke(cli, ["download", "--help"])
    assert help_text.exit_code == 0, help_text.output
    assert "gec_2" in help_text.output
    assert "file_tif" in help_text.output


@responses.activate
def test_download_asset_url_accepts_a_stem(tmp_path, monkeypatch):
    """A bare stem must resolve like get_item, not fail with 'No scheme supplied'."""
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))

    def boom(url, *args, **kwargs):
        raise AssertionError(f"No scheme supplied: fetched {url}")

    monkeypatch.setattr("umbra_py._http.get_json", boom)
    href = resolve_asset_name(_OPEN_DATA, path=db).asset_href("gec")
    responses.add(responses.HEAD, href, headers={"Content-Length": "8"}, status=200)
    out = download_asset(_OPEN_DATA, "gec", confirm=False)
    assert out["confirm_required"] is True
    assert out["asset"] == "gec"
    assert out["url"] == href

    monkeypatch.setenv("UMBRA_INDEX_DB", str(tmp_path / "missing.db"))
    with pytest.raises(ValueError, match="No local catalog index"):
        download_asset(_KHAVDA, "GEC")


def test_get_item_resolves_filename_stem_and_errors(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))

    card = get_item(f"{_KHAVDA}_GEC.tif")
    assert card["id"] == "khavda-2025-05-17"
    assert get_item(_TASK_DATA)["id"] == "task-data-2025-12-18"
    assert get_item(_OPEN_DATA)["id"] == "open-data-2026-01-02"

    with pytest.raises(ValueError, match="No acquisition"):
        get_item("2024-01-01-00-00-00_UMBRA-04_GEC.tif")

    monkeypatch.delenv("UMBRA_INDEX_DB")
    monkeypatch.setenv("UMBRA_INDEX_DB", str(tmp_path / "missing.db"))
    with pytest.raises(ValueError, match="No local catalog index"):
        get_item(_KHAVDA)

    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))
    copy = _item(
        _RED_SEA,
        "open-data/5026ca2c-0000-4000-8000-0000000000bb",
        "red-sea-copy",
    )
    with CatalogIndex(db) as idx:
        idx.add(copy)
        idx.commit()
    with pytest.raises(ValueError, match="2 acquisitions match") as exc:
        get_item(_RED_SEA)
    assert "sar-data/tasks/" in str(exc.value)
    assert "open-data/" in str(exc.value)


def test_get_item_filename_ignores_a_canopy_token(tmp_path, monkeypatch):
    db = tmp_path / "catalog.db"
    with _index(tmp_path):
        pass
    monkeypatch.setenv("UMBRA_INDEX_DB", str(db))
    monkeypatch.setattr("umbra_py.mcp_server._canopy_token", lambda: "secret")

    def boom(self, item_id):
        raise AssertionError(item_id)

    monkeypatch.setattr("umbra_py.mcp_server.UmbraCatalog.get_item", boom)
    assert get_item(f"{_OPEN_DATA}_GEC.tif")["id"] == "open-data-2026-01-02"
