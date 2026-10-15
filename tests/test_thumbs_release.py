"""Pack, upload, and check the chunked thumbnail release.

The 2026-10-12 publish deleted the live ``catalog.thumbs.db`` with
``gh release upload --clobber`` and then GitHub rejected the replacement
because the file was at least 2 GiB. Parts stay under that cap, and a failed
upload must not remove the previous manifest or the legacy sidecar.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import responses

from umbra_py.constants import CATALOG_THUMBS_ASSET, CATALOG_THUMBS_MANIFEST_ASSET
from umbra_py.exceptions import UmbraError
from umbra_py.thumbs_release import (
    GITHUB_RELEASE_ASSET_MAX_BYTES,
    THUMBS_PART_BYTES,
    ThumbnailFloors,
    assemble_thumbnail_sidecar,
    assert_remote_thumbnail_release,
    decide_thumbnail_publish,
    indexed_preview_count,
    pack_thumbnail_release,
    parse_thumbs_manifest,
    preview_count_from_health,
    publish_thumbnail_release,
    remembered_preview_count,
    remote_thumbnail_problems,
    sqlite_preview_count,
    stale_thumbs_asset_names,
    thumbnail_publish_line,
)


def test_part_budget_is_under_the_github_asset_limit():
    assert 0 < THUMBS_PART_BYTES < GITHUB_RELEASE_ASSET_MAX_BYTES


def test_pack_round_trip_is_byte_identical(tmp_path):
    src = tmp_path / "catalog.thumbs.db"
    src.write_bytes(bytes(range(256)) * 11)  # 2816 bytes, three parts at 1000
    manifest = pack_thumbnail_release(src, tmp_path / "out", part_bytes=1000)
    assert manifest["size"] == 2816
    assert manifest["sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    assert [part["size"] for part in manifest["parts"]] == [1000, 1000, 816]
    assert all(part["size"] < GITHUB_RELEASE_ASSET_MAX_BYTES for part in manifest["parts"])
    dest = tmp_path / "reassembled.db"
    assemble_thumbnail_sidecar(manifest, tmp_path / "out", dest)
    assert dest.read_bytes() == src.read_bytes()


def test_pack_refuses_a_part_size_github_would_reject(tmp_path):
    src = tmp_path / "catalog.thumbs.db"
    src.write_bytes(b"png")
    with pytest.raises(UmbraError, match="2147483648"):
        pack_thumbnail_release(src, tmp_path / "out", part_bytes=GITHUB_RELEASE_ASSET_MAX_BYTES)


def test_manifest_rejects_a_part_name_that_escapes_the_directory(tmp_path):
    src = tmp_path / "catalog.thumbs.db"
    src.write_bytes(b"x" * 10)
    manifest = pack_thumbnail_release(src, tmp_path / "out", part_bytes=100)
    manifest["parts"][0]["name"] = "../catalog.thumbs.db.part001." + "a" * 16
    with pytest.raises(UmbraError, match="part name"):
        parse_thumbs_manifest(manifest)


class _Release:
    """In-memory stand-in for ``gh release``."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.uploads: list[tuple[str, bool]] = []
        self.deleted: list[str] = []
        self.fail_after_delete: set[str] = set()
        self.fail_names: set[str] = set()

    def list_assets(self) -> list[dict[str, object]]:
        return [{"name": name, "size": len(data)} for name, data in self.files.items()]

    def upload(self, path: Path, *, clobber: bool) -> None:
        name = path.name
        self.uploads.append((name, clobber))
        data = path.read_bytes()
        if name in self.fail_names:
            raise UmbraError(f"upload {name} failed")
        if name in self.fail_after_delete:
            self.fail_after_delete.remove(name)
            self.files.pop(name, None)
            raise UmbraError(f"upload {name} failed after delete")
        if not clobber and name in self.files and self.files[name] != data:
            raise UmbraError(f"{name} already exists")
        self.files[name] = data

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.files.pop(name, None)

    def download(self, name: str, dest: Path) -> None:
        if name not in self.files:
            raise UmbraError(f"no asset {name}")
        dest.write_bytes(self.files[name])


def _packed(tmp_path: Path) -> tuple[Path, dict]:
    src = tmp_path / "catalog.thumbs.db"
    src.write_bytes(b"thumbnail-bytes" * 50)
    directory = tmp_path / "release"
    manifest = pack_thumbnail_release(src, directory, part_bytes=200)
    return directory, manifest


def test_publish_uploads_parts_before_the_manifest_and_then_drops_the_legacy_asset(tmp_path):
    directory, manifest = _packed(tmp_path)
    old_part = "catalog.thumbs.db.part001." + "ab" * 8
    client = _Release(
        {
            CATALOG_THUMBS_ASSET: b"stale-sidecar",
            old_part: b"stale-part",
            "catalog.db": b"index",
        }
    )
    # One part is already the bytes we would upload: do not send it again.
    first = manifest["parts"][0]["name"]
    client.files[first] = (directory / first).read_bytes()

    publish_thumbnail_release(directory, client=client)

    part_uploads = [
        (name, clobber) for name, clobber in client.uploads if name != CATALOG_THUMBS_MANIFEST_ASSET
    ]
    assert first not in {name for name, _ in part_uploads}
    assert part_uploads and all(clobber is False for _, clobber in part_uploads)
    manifest_at = next(
        i for i, (name, _) in enumerate(client.uploads) if name == CATALOG_THUMBS_MANIFEST_ASSET
    )
    assert all(name != CATALOG_THUMBS_MANIFEST_ASSET for name, _ in client.uploads[:manifest_at])
    assert client.uploads[manifest_at][1] is True
    assert client.deleted.index(CATALOG_THUMBS_ASSET) >= 0
    assert old_part in client.deleted
    assert "catalog.db" not in client.deleted
    assert CATALOG_THUMBS_ASSET not in client.files
    assert_remote_thumbnail_release(client=client)
    saved = json.loads(client.files[CATALOG_THUMBS_MANIFEST_ASSET])
    assert [part["name"] for part in saved["parts"]] == [part["name"] for part in manifest["parts"]]


def test_failed_part_upload_leaves_the_previous_release_alone(tmp_path):
    directory, manifest = _packed(tmp_path)
    client = _Release({CATALOG_THUMBS_ASSET: b"keep-me"})
    client.fail_names.add(manifest["parts"][1]["name"])
    with pytest.raises(UmbraError, match="failed"):
        publish_thumbnail_release(directory, client=client)
    assert client.files[CATALOG_THUMBS_ASSET] == b"keep-me"
    assert client.deleted == []
    assert CATALOG_THUMBS_MANIFEST_ASSET not in client.files


def test_failed_manifest_upload_restores_the_previous_manifest(tmp_path):
    directory, _manifest = _packed(tmp_path)
    previous = b'{"version": 1, "kept": true}\n'
    client = _Release({CATALOG_THUMBS_MANIFEST_ASSET: previous, CATALOG_THUMBS_ASSET: b"keep"})
    client.fail_after_delete.add(CATALOG_THUMBS_MANIFEST_ASSET)
    with pytest.raises(UmbraError, match="failed after delete"):
        publish_thumbnail_release(directory, client=client)
    assert client.files[CATALOG_THUMBS_MANIFEST_ASSET] == previous
    assert client.files[CATALOG_THUMBS_ASSET] == b"keep"
    assert client.deleted == []


def test_a_part_already_on_the_release_at_the_wrong_size_is_not_replaced(tmp_path):
    directory, manifest = _packed(tmp_path)
    name = manifest["parts"][0]["name"]
    client = _Release({name: b"not-the-part", CATALOG_THUMBS_ASSET: b"keep"})
    with pytest.raises(UmbraError, match="Refusing to replace"):
        publish_thumbnail_release(directory, client=client)
    assert client.uploads == []
    assert client.files[CATALOG_THUMBS_ASSET] == b"keep"


def test_remote_check_accepts_a_legacy_asset_and_rejects_a_hole(tmp_path):
    assert (
        remote_thumbnail_problems([{"name": CATALOG_THUMBS_ASSET, "size": 12}], client=None) == []
    )
    missing = remote_thumbnail_problems([], client=None)
    assert CATALOG_THUMBS_MANIFEST_ASSET in missing[0]
    too_big = remote_thumbnail_problems(
        [{"name": CATALOG_THUMBS_ASSET, "size": GITHUB_RELEASE_ASSET_MAX_BYTES}], client=None
    )
    assert too_big

    directory, manifest = _packed(tmp_path)
    assets = [
        {"name": CATALOG_THUMBS_MANIFEST_ASSET, "size": 10},
        {"name": manifest["parts"][0]["name"], "size": manifest["parts"][0]["size"]},
    ]
    # Name and size only: a later part that was never uploaded is a failed publish.
    from umbra_py.thumbs_release import thumbs_release_problems

    problems = thumbs_release_problems(manifest, assets)
    assert any(manifest["parts"][1]["name"] in problem for problem in problems)
    stale = stale_thumbs_asset_names(
        manifest,
        assets + [{"name": CATALOG_THUMBS_ASSET, "size": 3}, {"name": "catalog.db", "size": 4}],
    )
    assert stale == [CATALOG_THUMBS_ASSET]


def _sidecar(path: Path, n: int, png: bytes = b"png") -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE thumbnails (href TEXT PRIMARY KEY, png BLOB)")
    conn.executemany(
        "INSERT INTO thumbnails (href, png) VALUES (?, ?)",
        [(f"h{i}", png) for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_pack_records_the_sidecar_preview_count(tmp_path):
    src = tmp_path / "catalog.thumbs.db"
    _sidecar(src, 2)
    manifest = pack_thumbnail_release(src, tmp_path / "out", part_bytes=1000)
    assert manifest["previews"] == 2
    assert sqlite_preview_count(src) == 2


def test_manifest_rejects_a_non_integer_preview_count(tmp_path):
    src = tmp_path / "catalog.thumbs.db"
    src.write_bytes(b"x" * 10)
    manifest = pack_thumbnail_release(src, tmp_path / "out", part_bytes=100)
    assert "previews" not in manifest
    manifest["previews"] = "2"
    with pytest.raises(UmbraError, match="previews"):
        parse_thumbs_manifest(manifest)


def test_decide_thumbnail_publish_skips_a_count_below_the_floor():
    floors = ThumbnailFloors(manifest=100, legacy=None, healthz=4050, remembered=4050)
    decision = decide_thumbnail_publish(1500, floors)
    assert decision.publish is False
    assert decision.reason == "regression"
    assert decision.floor == 4050
    action, floor, reason = decision.line().split("\t", 2)
    assert action == "skip" and floor == "4050" and "Refusing to publish 1500" in reason

    same = decide_thumbnail_publish(4050, floors)
    assert same.publish is True and same.reason == "ok"
    assert same.line().split("\t", 2) == ["publish", "4050", "ok"]

    grown = decide_thumbnail_publish(4051, ThumbnailFloors(healthz=4050))
    assert grown.publish is True and grown.floor == 4050


def test_decide_thumbnail_publish_seeds_when_the_release_has_no_sidecar():
    """A growing bake may publish when nothing is on the release yet.

    The floor is still reported. A manifest, even a small one, keeps the
    shrink protection, and an empty bake is never a seed.
    """
    floors = ThumbnailFloors(healthz=3355, remembered=3355, release_absent=True)
    decision = decide_thumbnail_publish(1500, floors)
    assert decision.publish is True and decision.reason == "seed" and decision.floor == 3355
    action, floor, reason = decision.line().split("\t", 2)
    assert action == "publish" and floor == "3355"
    assert "seed" in reason and "will not replace" in reason

    caught_up = decide_thumbnail_publish(3355, floors)
    assert caught_up.publish is True and caught_up.reason == "ok"

    blocked = decide_thumbnail_publish(
        1500,
        ThumbnailFloors(manifest=100, healthz=3355, remembered=3355, release_absent=True),
    )
    assert blocked.publish is False and blocked.reason == "regression"

    empty = decide_thumbnail_publish(0, floors)
    assert empty.publish is False and empty.reason == "empty"


def test_decide_thumbnail_publish_skips_empty_and_warns_when_no_floor_is_known():
    empty = decide_thumbnail_publish(0, ThumbnailFloors(healthz=4050))
    assert empty.publish is False and empty.reason == "empty"
    assert empty.line().startswith("skip\t4050\t")

    unknown = decide_thumbnail_publish(12, ThumbnailFloors())
    assert unknown.publish is True and unknown.reason == "no-floor"
    action, floor, reason = unknown.line().split("\t", 2)
    assert action == "publish" and floor == "" and "No previous" in reason

    with pytest.raises(UmbraError, match="new_count"):
        decide_thumbnail_publish(True, ThumbnailFloors())  # type: ignore[arg-type]


def test_healthz_preview_count_ignores_a_missing_or_boolean_count():
    assert preview_count_from_health({"thumbnails": {"count": 4050}}) == 4050
    assert preview_count_from_health({"thumbnails": {"count": 0}}) == 0
    assert preview_count_from_health({"thumbnails": {"count": True}}) is None
    assert preview_count_from_health({"status": "ok"}) is None
    assert preview_count_from_health([]) is None


def test_remembered_floor_file_holds_the_live_preview_count():
    path = Path(__file__).resolve().parents[1] / ".github" / "thumbnail-preview-floor.json"
    count, note = remembered_preview_count(path)
    assert note is None
    assert count == 3355


_MANIFEST = "https://example.test/thumbs/catalog.thumbs.manifest.json"
_LEGACY = "https://example.test/thumbs/catalog.thumbs.db"
_HEALTH = "https://example.test/healthz"


def _line(sidecar: Path, floor: Path) -> str:
    return thumbnail_publish_line(
        sidecar, floor, manifest_url=_MANIFEST, legacy_url=_LEGACY, healthz_url=_HEALTH
    )


@responses.activate
def test_publish_line_seeds_when_the_release_is_confirmed_empty(tmp_path):
    """404 on both release assets may publish below /healthz. A 503 may not."""
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar(sidecar, 2)
    floor = tmp_path / "floor.json"
    floor.write_text(json.dumps({"previews": 4050}), encoding="utf-8")
    responses.add(responses.GET, _MANIFEST, status=404)
    responses.add(responses.HEAD, _LEGACY, status=404)
    responses.add(responses.GET, _HEALTH, json={"thumbnails": {"count": 4050}}, status=200)
    action, got, reason = _line(sidecar, floor).split("\t", 2)
    assert action == "publish" and got == "4050"
    assert "seed" in reason and "healthz=4050" in reason and "remembered=4050" in reason
    assert not any(
        call.request.url == _LEGACY and call.request.method == "GET" for call in responses.calls
    )

    responses.reset()
    responses.add(responses.GET, _MANIFEST, status=503)
    responses.add(responses.HEAD, _LEGACY, status=503)
    responses.add(responses.GET, _HEALTH, json={"thumbnails": {"count": 4050}}, status=200)
    action, got, reason = _line(sidecar, floor).split("\t", 2)
    assert action == "skip" and got == "4050"
    assert "Refusing" in reason


@responses.activate
def test_publish_line_counts_a_legacy_sidecar_when_the_manifest_is_absent(tmp_path):
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar(sidecar, 2)
    previous = tmp_path / "previous.db"
    _sidecar(previous, 4, png=b"old")
    body = previous.read_bytes()
    headers = {"Content-Length": str(len(body))}
    responses.add(responses.GET, _MANIFEST, status=404)
    responses.add(responses.HEAD, _LEGACY, status=200, headers=headers)
    responses.add(responses.GET, _LEGACY, body=body, status=200, headers=headers)
    responses.add(responses.GET, _HEALTH, status=503)
    floor = tmp_path / "floor.json"
    floor.write_text(json.dumps({"previews": 1}), encoding="utf-8")
    action, got, reason = _line(sidecar, floor).split("\t", 2)
    assert action == "skip" and got == "4"
    assert "legacy=4" in reason


@responses.activate
def test_publish_line_reads_previews_from_the_manifest_without_fetching_parts(tmp_path):
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar(sidecar, 9)
    responses.add(responses.GET, _MANIFEST, json={"version": 1, "previews": 9}, status=200)
    responses.add(responses.GET, _HEALTH, json={"thumbnails": {"count": 1}}, status=200)
    floor = tmp_path / "floor.json"
    floor.write_text(json.dumps({"previews": 1}), encoding="utf-8")
    action, got, reason = _line(sidecar, floor).split("\t", 2)
    assert (action, got, reason) == ("publish", "9", "ok")
    assert not any(call.request.url == _LEGACY for call in responses.calls)


@responses.activate
def test_publish_line_warns_and_allows_a_first_publish_when_every_floor_is_unknown(tmp_path):
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar(sidecar, 3)
    responses.add(responses.GET, _MANIFEST, status=404)
    responses.add(responses.HEAD, _LEGACY, status=404)
    responses.add(responses.GET, _HEALTH, status=503)
    action, got, reason = _line(sidecar, tmp_path / "missing-floor.json").split("\t", 2)
    assert action == "publish" and got == ""
    assert "No previous" in reason and "unreadable" in reason


def _index_with_id(path, item_id: str, href: str) -> None:
    from umbra_py.index import CatalogIndex
    from umbra_py.models import UmbraItem

    with CatalogIndex(path) as idx:
        idx.add(
            UmbraItem.from_dict(
                {
                    "id": item_id,
                    "bbox": [0, 0, 1, 1],
                    "properties": {"datetime": "2024-01-15T10:00:00Z"},
                    "assets": {},
                },
                href=href,
            )
        )
        idx.commit()


def _sidecar_rows(path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE thumbnails (href TEXT PRIMARY KEY, id TEXT NOT NULL, png BLOB NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO thumbnails (href, id, png) VALUES (?, ?, ?)",
        [(href, item_id, b"png") for href, item_id in rows],
    )
    conn.commit()
    conn.close()


def test_indexed_preview_count_drops_ids_the_index_no_longer_has(tmp_path):
    index = tmp_path / "catalog.db"
    href = "https://example.test/keep.json"
    _index_with_id(index, "keep", href)
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar_rows(sidecar, [(href, "keep"), ("https://example.test/drop.json", "drop")])
    assert sqlite_preview_count(sidecar) == 2
    assert indexed_preview_count(sidecar, index) == 1
    assert indexed_preview_count(tmp_path / "missing.db", index) is None


@responses.activate
def test_publish_line_counts_only_previews_that_match_the_index(tmp_path):
    """A legacy sidecar's stale rows do not raise the floor above the index."""
    href = "https://example.test/keep.json"
    index = tmp_path / "catalog.db"
    _index_with_id(index, "keep", href)
    sidecar = tmp_path / "catalog.thumbs.db"
    _sidecar_rows(sidecar, [(href, "keep")])
    previous = tmp_path / "previous.db"
    _sidecar_rows(
        previous,
        [(href, "keep"), ("https://example.test/drop.json", "drop")],
    )
    body = previous.read_bytes()
    headers = {"Content-Length": str(len(body))}
    responses.add(responses.GET, _MANIFEST, status=404)
    responses.add(responses.HEAD, _LEGACY, status=200, headers=headers)
    responses.add(responses.GET, _LEGACY, body=body, status=200, headers=headers)
    responses.add(responses.GET, _HEALTH, status=503)
    floor = tmp_path / "floor.json"
    floor.write_text(json.dumps({"previews": 1}), encoding="utf-8")

    def line(index_path):
        return thumbnail_publish_line(
            sidecar,
            floor,
            index=index_path,
            manifest_url=_MANIFEST,
            legacy_url=_LEGACY,
            healthz_url=_HEALTH,
        )

    action, got, reason = line(index).split("\t", 2)
    assert (action, got) == ("publish", "1")
    assert reason == "ok"

    responses.reset()
    responses.add(responses.GET, _MANIFEST, status=404)
    responses.add(responses.HEAD, _LEGACY, status=200, headers=headers)
    responses.add(responses.GET, _LEGACY, body=body, status=200, headers=headers)
    responses.add(responses.GET, _HEALTH, status=503)
    action, got, reason = line(None).split("\t", 2)
    assert action == "skip" and got == "2"
    assert "legacy=2" in reason
