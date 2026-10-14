"""Publish ``catalog.thumbs.db`` as GitHub release assets that fit.

GitHub rejects a release asset of 2 GiB or larger (HTTP 422 ``size must be
less than 2147483648``). The baked sidecar crossed that line: on 2026-10-12
the weekly workflow exported 5,550 previews and ``gh release upload --clobber``
deleted the previous ``catalog.thumbs.db`` before the upload was rejected, so
the release was left with no thumbnail asset at all. ``--clobber`` always
deletes first; the ``gh`` help says a failed upload then loses the original.

PNG bytes do not shrink usefully under gzip, and the archive is still growing,
so the sidecar is split into hash-named parts under 1.5 GiB. A small
``catalog.thumbs.manifest.json`` is uploaded last and is what
``umbra index fetch-thumbnails`` reads. Part names include the content hash,
so a rerun does not ``--clobber`` a multi-gigabyte asset. The previous manifest
is backed up and restored if replacing it fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .constants import (
    CATALOG_INDEX_RELEASE,
    CATALOG_THUMBS_ASSET,
    CATALOG_THUMBS_MANIFEST_ASSET,
)
from .exceptions import UmbraError

#: GitHub's upload API rejects an asset whose size is not strictly smaller.
GITHUB_RELEASE_ASSET_MAX_BYTES = 2 * 1024 * 1024 * 1024

#: Each published part stays under the cap with room to spare. 1.5 GiB is a
#: raw byte slice of the sqlite file; the downloader concatenates the slices
#: back to the original bytes.
THUMBS_PART_BYTES = 1536 * 1024 * 1024

_READ = 1024 * 1024


def pack_thumbnail_release(
    src: str | os.PathLike,
    dest_dir: str | os.PathLike,
    *,
    part_bytes: int = THUMBS_PART_BYTES,
) -> dict[str, Any]:
    """Split ``src`` into release parts and write ``catalog.thumbs.manifest.json``.

    ``part_bytes`` must be strictly under :data:`GITHUB_RELEASE_ASSET_MAX_BYTES`.
    Part files are named ``catalog.thumbs.db.partNNN.<sha256[:16]>`` and sit
    next to the manifest in ``dest_dir``. Returns the manifest object.
    """
    if part_bytes < 1 or part_bytes >= GITHUB_RELEASE_ASSET_MAX_BYTES:
        raise UmbraError(
            f"part_bytes must be in 1..{GITHUB_RELEASE_ASSET_MAX_BYTES - 1}; "
            "GitHub rejects a release asset of "
            f"{GITHUB_RELEASE_ASSET_MAX_BYTES} bytes or more."
        )
    source = Path(src)
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_size = source.stat().st_size
    if total_size <= 0:
        raise UmbraError(
            f"{source} is empty; refusing to publish a thumbnail sidecar with no bytes."
        )

    total = hashlib.sha256()
    parts: list[dict[str, Any]] = []
    remaining = total_size
    with source.open("rb") as fh:
        part_index = 0
        while remaining:
            part_index += 1
            to_write = min(part_bytes, remaining)
            part_hash = hashlib.sha256()
            partial = out_dir / f".part{part_index:03d}.partial"
            try:
                with partial.open("wb") as out:
                    left = to_write
                    while left:
                        buf = fh.read(min(_READ, left))
                        if not buf:
                            raise UmbraError(
                                f"{source} ended early: "
                                f"{total_size - remaining} of {total_size} bytes."
                            )
                        out.write(buf)
                        part_hash.update(buf)
                        total.update(buf)
                        left -= len(buf)
                digest = part_hash.hexdigest()
                name = f"catalog.thumbs.db.part{part_index:03d}.{digest[:16]}"
                partial.replace(out_dir / name)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            parts.append({"name": name, "size": to_write, "sha256": digest})
            remaining -= to_write

    manifest = {
        "version": 1,
        "asset": CATALOG_THUMBS_ASSET,
        "size": total_size,
        "sha256": total.hexdigest(),
        "parts": parts,
    }
    parse_thumbs_manifest(manifest)
    (out_dir / CATALOG_THUMBS_MANIFEST_ASSET).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def parse_thumbs_manifest(manifest: Any) -> dict[str, Any]:
    """Return ``manifest`` if it is a thumbnail release manifest this build can reassemble."""
    if not isinstance(manifest, dict):
        raise UmbraError("Thumbnail manifest is not a JSON object.")
    if manifest.get("version") != 1:
        raise UmbraError(
            f"Thumbnail manifest version {manifest.get('version')!r} is not supported."
        )
    if manifest.get("asset") != CATALOG_THUMBS_ASSET:
        raise UmbraError(
            f"Thumbnail manifest asset {manifest.get('asset')!r} is not {CATALOG_THUMBS_ASSET}."
        )
    size = manifest.get("size")
    sha = manifest.get("sha256")
    parts = manifest.get("parts")
    if not isinstance(size, int) or size <= 0:
        raise UmbraError(f"Thumbnail manifest size {size!r} is not a positive integer.")
    if not _is_sha256(sha):
        raise UmbraError("Thumbnail manifest sha256 is not 64 hex characters.")
    if not isinstance(parts, list) or not parts:
        raise UmbraError("Thumbnail manifest has no parts.")
    seen: set[str] = set()
    covered = 0
    for part in parts:
        if not isinstance(part, dict):
            raise UmbraError("Thumbnail manifest part is not an object.")
        name = part.get("name")
        part_size = part.get("size")
        part_sha = part.get("sha256")
        if not isinstance(name, str) or not _safe_part_name(name):
            raise UmbraError(f"Thumbnail manifest part name {name!r} is not a sidecar part.")
        if name in seen:
            raise UmbraError(f"Thumbnail manifest repeats part {name}.")
        seen.add(name)
        if (
            not isinstance(part_size, int)
            or part_size <= 0
            or part_size >= GITHUB_RELEASE_ASSET_MAX_BYTES
        ):
            raise UmbraError(
                f"{name} is {part_size!r} bytes; each part must be under "
                f"{GITHUB_RELEASE_ASSET_MAX_BYTES} bytes."
            )
        if not _is_sha256(part_sha):
            raise UmbraError(f"{name} sha256 is not 64 hex characters.")
        covered += part_size
    if covered != size:
        raise UmbraError(f"Thumbnail manifest parts cover {covered} bytes; size is {size}.")
    return manifest


def append_verified_part(part_path: Path, dest: BinaryIO, part: dict[str, Any], total: Any) -> None:
    """Append ``part_path`` to ``dest`` after checking the manifest's size and sha256.

    ``total`` is a running hasher of the concatenation (a ``hashlib.sha256()``).
    """
    digest = hashlib.sha256()
    size = 0
    with part_path.open("rb") as src:
        while True:
            buf = src.read(_READ)
            if not buf:
                break
            digest.update(buf)
            total.update(buf)
            dest.write(buf)
            size += len(buf)
    actual = digest.hexdigest()
    if size != part["size"] or actual != part["sha256"]:
        raise UmbraError(
            f"{part['name']} is {size} bytes sha256 {actual}; "
            f"manifest says {part['size']} bytes sha256 {part['sha256']}."
        )


def assemble_thumbnail_sidecar(
    manifest: dict[str, Any], part_dir: str | os.PathLike, dest: str | os.PathLike
) -> None:
    """Concatenate ``manifest``'s parts from ``part_dir`` into ``dest``.

    The result is byte-identical to the file :func:`pack_thumbnail_release` split.
    A mismatch deletes the partial output and raises :class:`UmbraError`.
    """
    checked = parse_thumbs_manifest(manifest)
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    total = hashlib.sha256()
    try:
        with partial.open("wb") as out:
            for part in checked["parts"]:
                append_verified_part(Path(part_dir) / part["name"], out, part, total)
        if total.hexdigest() != checked["sha256"]:
            raise UmbraError(
                "Reassembled thumbnail sidecar sha256 "
                f"{total.hexdigest()} != manifest {checked['sha256']}."
            )
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def thumbs_release_problems(manifest: dict[str, Any], assets: list[dict[str, Any]]) -> list[str]:
    """Why ``assets`` (``gh release view --json assets``) do not match ``manifest``."""
    try:
        checked = parse_thumbs_manifest(manifest)
    except UmbraError as exc:
        return [str(exc)]
    by_name = {asset.get("name"): asset.get("size") for asset in assets}
    problems: list[str] = []
    if CATALOG_THUMBS_MANIFEST_ASSET not in by_name:
        problems.append(f"{CATALOG_THUMBS_MANIFEST_ASSET} is not on the release")
    for part in checked["parts"]:
        size = by_name.get(part["name"])
        if size is None:
            problems.append(f"{part['name']} is not on the release")
        elif size != part["size"]:
            problems.append(
                f"{part['name']} is {size} bytes on the release; manifest says {part['size']}"
            )
    return problems


def stale_thumbs_asset_names(manifest: dict[str, Any], assets: list[dict[str, Any]]) -> list[str]:
    """Thumbnail assets on the release that this manifest does not name.

    The legacy single ``catalog.thumbs.db`` and parts from an earlier bake.
    Other release files (``catalog.db``, parquet, pmtiles) are left alone.
    """
    keep = {CATALOG_THUMBS_MANIFEST_ASSET, *(part["name"] for part in manifest["parts"])}
    stale: list[str] = []
    for asset in assets:
        name = asset.get("name")
        if not isinstance(name, str) or name in keep:
            continue
        if name == CATALOG_THUMBS_ASSET or name.startswith("catalog.thumbs.db.part"):
            stale.append(name)
    return stale


class GhRelease:
    """The ``gh release`` calls the publish step needs. Tests substitute their own."""

    def __init__(self, tag: str = CATALOG_INDEX_RELEASE) -> None:
        self.tag = tag

    def list_assets(self) -> list[dict[str, Any]]:
        payload = json.loads(self._run(["release", "view", self.tag, "--json", "assets"]))
        assets = payload["assets"]
        if not isinstance(assets, list):
            raise UmbraError("`gh release view --json assets` did not return a list.")
        return assets

    def upload(self, path: Path, *, clobber: bool) -> None:
        args = ["release", "upload", self.tag, str(path)]
        if clobber:
            args.append("--clobber")
        self._run(args)

    def delete(self, name: str) -> None:
        self._run(["release", "delete-asset", self.tag, name, "--yes"])

    def download(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            ["release", "download", self.tag, "--pattern", name, "--clobber", "-O", str(dest)]
        )

    def _run(self, args: list[str]) -> str:
        proc = subprocess.run(["gh", *args], check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise UmbraError(f"`gh {' '.join(args)}` exited {proc.returncode}: {detail}")
        return proc.stdout


def publish_thumbnail_release(release_dir: str | os.PathLike, *, client: Any = None) -> None:
    """Upload a packed thumbnail release without deleting a live asset first.

    Parts are uploaded only when that exact name is absent. A name that is
    already present at a different size is a refusal, not a ``--clobber``.
    The manifest is uploaded last, and only that small file is replaced in
    place. If that replacement fails, the previous manifest is uploaded again.
    Stale thumbnail assets are deleted only after the new manifest is on the
    release and its parts match.
    """
    client = client or GhRelease()
    directory = Path(release_dir)
    manifest_path = directory / CATALOG_THUMBS_MANIFEST_ASSET
    manifest = parse_thumbs_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
    assets = client.list_assets()
    by_name = {asset.get("name"): asset.get("size") for asset in assets}

    for part in manifest["parts"]:
        name = part["name"]
        local = directory / name
        if not local.is_file():
            raise UmbraError(f"Missing part file {local}.")
        local_size = local.stat().st_size
        if local_size != part["size"]:
            raise UmbraError(f"{name} is {local_size} bytes on disk; manifest says {part['size']}.")
        remote = by_name.get(name)
        if remote == part["size"]:
            continue
        if remote is not None:
            raise UmbraError(
                f"{name} is already on the release at {remote} bytes, not {part['size']}. "
                "Refusing to replace it."
            )
        client.upload(local, clobber=False)

    previous = _backup_manifest(client, assets)
    try:
        client.upload(manifest_path, clobber=True)
    except UmbraError as upload_error:
        if previous is None:
            raise
        try:
            _restore_manifest(client, previous)
        except UmbraError as restore_error:
            raise UmbraError(
                f"{upload_error} Restoring the previous manifest also failed: {restore_error}"
            ) from upload_error
        raise

    problems = thumbs_release_problems(manifest, client.list_assets())
    if problems:
        raise UmbraError("Thumbnail release check failed: " + "; ".join(problems))
    for name in stale_thumbs_asset_names(manifest, client.list_assets()):
        client.delete(name)


def assert_remote_thumbnail_release(*, client: Any = None) -> None:
    """Fail if the rolling release has no usable thumbnail sidecar.

    A manifest must name parts that are present at the recorded sizes, and the
    legacy ``catalog.thumbs.db`` asset must not still be published beside it
    (older clients would fetch that stale file). With no manifest, a single
    ``catalog.thumbs.db`` under the 2 GiB cap is accepted.
    """
    client = client or GhRelease()
    problems = remote_thumbnail_problems(client.list_assets(), client)
    if problems:
        raise UmbraError("Thumbnail release is missing or incomplete: " + "; ".join(problems))


def remote_thumbnail_problems(assets: list[dict[str, Any]], client: Any) -> list[str]:
    """Problems with the thumbnail assets currently on the release. Empty means usable."""
    by_name = {asset.get("name"): asset.get("size") for asset in assets}
    if CATALOG_THUMBS_MANIFEST_ASSET not in by_name:
        size = by_name.get(CATALOG_THUMBS_ASSET)
        if not isinstance(size, int) or size <= 0 or size >= GITHUB_RELEASE_ASSET_MAX_BYTES:
            return [
                "neither "
                f"{CATALOG_THUMBS_MANIFEST_ASSET} nor a usable {CATALOG_THUMBS_ASSET} "
                f"is on the release (legacy asset size: {size!r})"
            ]
        return []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CATALOG_THUMBS_MANIFEST_ASSET
        client.download(CATALOG_THUMBS_MANIFEST_ASSET, path)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return [f"{CATALOG_THUMBS_MANIFEST_ASSET} is not JSON: {exc}"]
    problems = thumbs_release_problems(manifest, assets)
    if CATALOG_THUMBS_ASSET in by_name:
        problems.append(
            f"{CATALOG_THUMBS_ASSET} is still on the release beside the manifest; "
            "older clients would download that stale sidecar"
        )
    return problems


def _backup_manifest(client: Any, assets: list[dict[str, Any]]) -> bytes | None:
    if not any(asset.get("name") == CATALOG_THUMBS_MANIFEST_ASSET for asset in assets):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CATALOG_THUMBS_MANIFEST_ASSET
        try:
            client.download(CATALOG_THUMBS_MANIFEST_ASSET, path)
        except UmbraError as exc:
            raise UmbraError(
                "Could not back up the published thumbnail manifest before replacing it: "
                + str(exc)
            ) from exc
        return path.read_bytes()


def _restore_manifest(client: Any, previous: bytes) -> None:
    """Upload ``previous`` back under the stable manifest asset name."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / CATALOG_THUMBS_MANIFEST_ASSET
        path.write_bytes(previous)
        client.upload(path, clobber=True)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _safe_part_name(name: str) -> bool:
    return (
        name.startswith("catalog.thumbs.db.part")
        and len(name) <= 80
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )
