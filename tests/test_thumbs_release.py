"""Pack, upload, and check the chunked thumbnail release.

The 2026-10-12 publish deleted the live ``catalog.thumbs.db`` with
``gh release upload --clobber`` and then GitHub rejected the replacement
because the file was at least 2 GiB. Parts stay under that cap, and a failed
upload must not remove the previous manifest or the legacy sidecar.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from umbra_py.constants import CATALOG_THUMBS_ASSET, CATALOG_THUMBS_MANIFEST_ASSET
from umbra_py.exceptions import UmbraError
from umbra_py.thumbs_release import (
    GITHUB_RELEASE_ASSET_MAX_BYTES,
    THUMBS_PART_BYTES,
    assemble_thumbnail_sidecar,
    assert_remote_thumbnail_release,
    pack_thumbnail_release,
    parse_thumbs_manifest,
    publish_thumbnail_release,
    remote_thumbnail_problems,
    stale_thumbs_asset_names,
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
