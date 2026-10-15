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
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .constants import (
    CATALOG_INDEX_RELEASE,
    CATALOG_INDEX_THUMBS_MANIFEST_URL,
    CATALOG_INDEX_THUMBS_URL,
    CATALOG_THUMBS_ASSET,
    CATALOG_THUMBS_MANIFEST_ASSET,
)
from .exceptions import UmbraError

#: ``GET /healthz`` on the public community API. ``thumbnails.count`` is the
#: number of baked previews the running server has merged, which is the floor
#: the weekly publish compares against when the release itself has no sidecar.
PUBLIC_HEALTHZ_URL = "https://api.umbra-py.space/healthz"

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
    # A real sidecar records how many previews it holds so the next publish
    # can refuse a smaller one without downloading the parts. A file that is
    # not a thumbnail database (tests pack raw bytes) omits the field.
    previews = sqlite_preview_count(source)
    if previews is not None:
        manifest["previews"] = previews
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
    if "previews" in manifest:
        previews = manifest.get("previews")
        if _nonneg_int(previews) is None:
            raise UmbraError(
                f"Thumbnail manifest previews {previews!r} is not a non-negative integer."
            )
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


def _nonneg_int(value: Any) -> int | None:
    """``value`` when it is an ``int`` >= 0. ``bool`` is not an int here."""
    if type(value) is int and value >= 0:
        return value
    return None


def sqlite_preview_count(path: str | os.PathLike) -> int | None:
    """``COUNT(*)`` of a thumbnail sidecar, or ``None`` when ``path`` is not one.

    A missing file, a non-database, or a database with no ``thumbnails`` table
    is ``None`` — the caller cannot treat that as zero previews. Zero is a
    real count of an empty ``thumbnails`` table.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM thumbnails").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return _nonneg_int(row[0])


def indexed_preview_count(sidecar: str | os.PathLike, index_path: str | os.PathLike) -> int | None:
    """How many sidecar previews belong to an item still in ``index_path``.

    A row counts when its ``id`` is in the index or its ``href`` is. Quarantined
    items and duplicate hrefs whose id was dropped are not in the index, so
    they do not count. ``None`` means ``sidecar`` is not a thumbnail database
    or ``index_path`` could not be read — not zero previews. Zero means none of
    the rows match.
    """
    try:
        conn = sqlite3.connect(f"file:{sidecar}?mode=ro", uri=True)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(thumbnails)")}
            clauses: list[str] = []
            if "id" in columns:
                clauses.append("t.id IN (SELECT id FROM idx.items)")
            if "href" in columns:
                clauses.append("t.href IN (SELECT href FROM idx.items)")
            if not clauses:
                return None
            conn.execute("ATTACH DATABASE ? AS idx", (f"file:{index_path}?mode=ro",))
            row = conn.execute(
                f"SELECT COUNT(*) FROM thumbnails AS t WHERE {' OR '.join(clauses)}"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    if row is None:
        return None
    return _nonneg_int(row[0])


def _count_previews(path: str | os.PathLike, index: str | os.PathLike | None) -> int | None:
    """Indexed overlap when ``index`` is set and readable, else the raw row count."""
    if index is not None:
        counted = indexed_preview_count(path, index)
        if counted is not None:
            return counted
    return sqlite_preview_count(path)


@dataclass(frozen=True)
class ThumbnailFloors:
    """Preview counts the publish guard was able to read.

    ``None`` means that source could not be read (absent, unreachable, or
    carrying no count). A present zero is a real count and participates in
    the floor. ``notes`` are the failures a warning should mention; a 404
    on an asset that is simply not published is not a note.

    ``release_absent`` is true only when the manifest and the legacy sidecar
    are both confirmed missing (HTTP 404). A network error is not absence:
    the guard must not treat an unread release as an empty one.
    """

    manifest: int | None = None
    legacy: int | None = None
    healthz: int | None = None
    remembered: int | None = None
    notes: tuple[str, ...] = ()
    release_absent: bool = False

    def floor(self) -> int | None:
        known = [
            count
            for count in (self.manifest, self.legacy, self.healthz, self.remembered)
            if count is not None
        ]
        return max(known) if known else None


@dataclass(frozen=True)
class ThumbnailPublishDecision:
    """Whether a freshly baked sidecar may replace the published thumbnail assets.

    ``publish`` is false for an empty sidecar and for one with fewer previews
    than :meth:`ThumbnailFloors.floor`, except when the release has no sidecar
    at all: a positive count may be published as a seed (``reason`` ``seed``).
    The catalog index is a separate upload; a false decision leaves the
    previous thumbnail assets where they are. ``reason`` is ``empty``,
    ``regression``, ``no-floor``, ``seed``, or ``ok``.
    """

    publish: bool
    new_count: int
    floor: int | None
    reason: str
    sources: dict[str, int | None]
    notes: tuple[str, ...] = ()

    def message(self) -> str:
        if self.reason == "empty":
            return "No baked thumbnails to publish; leaving the released sidecar alone."
        if self.reason == "no-floor":
            detail = "; ".join(self.notes) if self.notes else "no release, /healthz, or repo floor"
            return (
                f"No previous thumbnail preview count ({detail}). "
                f"Publishing {self.new_count} previews."
            )
        if self.reason == "seed":
            known = ", ".join(
                f"{name}={count}" for name, count in self.sources.items() if count is not None
            )
            extra = f" Notes: {'; '.join(self.notes)}." if self.notes else ""
            return (
                f"Publishing {self.new_count} previews as a thumbnail seed; the release "
                f"has no sidecar (floor {self.floor}, {known}). The server will not "
                f"replace a larger local sidecar.{extra}"
            )
        if self.reason == "regression":
            known = ", ".join(
                f"{name}={count}" for name, count in self.sources.items() if count is not None
            )
            extra = f" Notes: {'; '.join(self.notes)}." if self.notes else ""
            return (
                f"Refusing to publish {self.new_count} previews; the floor is "
                f"{self.floor} ({known}). Previous thumbnail assets were left in "
                f"place. The catalog index publish is unaffected.{extra}"
            )
        return "ok"

    def line(self) -> str:
        """One tab-separated line: ``publish|skip``, floor, message.

        The floor field is empty when no previous count was known. The message
        has no tabs or newlines, so a shell ``read`` can split the line.
        """
        floor = "" if self.floor is None else str(self.floor)
        action = "publish" if self.publish else "skip"
        text = " ".join(self.message().split())
        return f"{action}\t{floor}\t{text}"


def _seeding(floors: ThumbnailFloors) -> bool:
    """True when a positive bake may be published because the release has no sidecar.

    The server-side refresh still refuses to replace a larger local file, so
    this seed cannot shrink the copy ``/healthz`` is counting.
    """
    return floors.release_absent and floors.manifest is None and floors.legacy is None


def decide_thumbnail_publish(new_count: int, floors: ThumbnailFloors) -> ThumbnailPublishDecision:
    """Decide whether ``new_count`` previews may replace the published sidecar.

    The floor is the greatest count among the manifest, the legacy asset, the
    live ``/healthz`` ``thumbnails.count``, and the repo-stored last-known
    count. Those counts are previews that match the current index when the
    caller could read the rows. A new sidecar below that floor is not
    published, unless the release has no sidecar at all: then a positive
    count is a seed (``reason`` ``seed``) and the known floor is only reported.
    When every source is unknown the sidecar may be published, and ``reason``
    is ``no-floor`` so the caller can warn. ``new_count`` of zero is never
    published.
    """
    if _nonneg_int(new_count) is None:
        raise UmbraError(f"new_count {new_count!r} is not a non-negative integer.")
    sources = {
        "manifest": floors.manifest,
        "legacy": floors.legacy,
        "healthz": floors.healthz,
        "remembered": floors.remembered,
    }
    floor = floors.floor()
    if new_count <= 0:
        reason = "empty"
        publish = False
    elif floor is None:
        reason = "no-floor"
        publish = True
    elif new_count < floor and _seeding(floors):
        reason = "seed"
        publish = True
    elif new_count < floor:
        reason = "regression"
        publish = False
    else:
        reason = "ok"
        publish = True
    return ThumbnailPublishDecision(publish, new_count, floor, reason, sources, floors.notes)


def preview_count_from_health(document: Any) -> int | None:
    """``thumbnails.count`` from a ``/healthz`` document, or ``None``."""
    if not isinstance(document, dict):
        return None
    thumbs = document.get("thumbnails")
    if not isinstance(thumbs, dict):
        return None
    return _nonneg_int(thumbs.get("count"))


def remembered_preview_count(path: str | os.PathLike) -> tuple[int | None, str | None]:
    """Read the repo-stored last-known preview count.

    Returns ``(count, note)``. ``note`` is set when the file cannot be used;
    a missing file is a note, not an exception, so a publish guard can fall
    through to the other floors.
    """
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"repo thumbnail floor unreadable: {exc}"
    if not isinstance(data, dict):
        return None, "repo thumbnail floor is not a JSON object"
    count = _nonneg_int(data.get("previews"))
    if count is None:
        return None, "repo thumbnail floor has no non-negative previews count"
    return count, None


def collect_thumbnail_floors(
    *,
    session: Any = None,
    floor_path: str | os.PathLike | None = None,
    index: str | os.PathLike | None = None,
    healthz_url: str = PUBLIC_HEALTHZ_URL,
    manifest_url: str = CATALOG_INDEX_THUMBS_MANIFEST_URL,
    legacy_url: str = CATALOG_INDEX_THUMBS_URL,
) -> ThumbnailFloors:
    """Read every preview-count floor, skipping sources that are unreachable.

    A manifest with a ``previews`` field is that count; the parts are not
    downloaded. With no manifest, a legacy ``catalog.thumbs.db`` is downloaded
    and counted. ``/healthz`` contributes ``thumbnails.count`` when the live
    API answers. ``floor_path`` is the repo-stored last-known count. When
    ``index`` is set, a downloaded legacy sidecar counts only rows whose id or
    href is in that index. The manifest's ``previews`` field is a number, not
    a row list, so it cannot be filtered the same way. Failures are notes, not
    exceptions: the index publish must not depend on a floor that happens to
    be down.
    """
    sess = session if session is not None else _session()
    notes: list[str] = []
    manifest_count, legacy_count, release_notes, release_absent = _release_counts(
        sess, manifest_url, legacy_url, index=index
    )
    notes.extend(release_notes)
    healthz_count, healthz_note = _healthz_count(sess, healthz_url)
    if healthz_note:
        notes.append(healthz_note)
    remembered: int | None = None
    if floor_path is not None:
        remembered, remembered_note = remembered_preview_count(floor_path)
        if remembered_note:
            notes.append(remembered_note)
    return ThumbnailFloors(
        manifest_count,
        legacy_count,
        healthz_count,
        remembered,
        tuple(notes),
        release_absent,
    )


def thumbnail_publish_line(
    sidecar: str | os.PathLike,
    floor_path: str | os.PathLike,
    *,
    index: str | os.PathLike | None = None,
    session: Any = None,
    healthz_url: str = PUBLIC_HEALTHZ_URL,
    manifest_url: str = CATALOG_INDEX_THUMBS_MANIFEST_URL,
    legacy_url: str = CATALOG_INDEX_THUMBS_URL,
) -> str:
    """One line the weekly workflow reads before uploading thumbnail assets.

    Counts ``sidecar`` and compares it with :func:`collect_thumbnail_floors`.
    When ``index`` is set, the new sidecar counts only previews whose id or
    href is in that index. A file that is not a thumbnail database counts as
    zero and is not published.
    """
    counted = _count_previews(sidecar, index)
    floors = collect_thumbnail_floors(
        session=session,
        floor_path=floor_path,
        index=index,
        healthz_url=healthz_url,
        manifest_url=manifest_url,
        legacy_url=legacy_url,
    )
    return decide_thumbnail_publish(0 if counted is None else counted, floors).line()


def _session() -> Any:
    from ._http import default_session  # noqa: PLC0415

    return default_session()


def _optional_get_json(session: Any, url: str) -> tuple[Any, int | None, str | None]:
    """``(body, status, note)`` for a JSON GET that may 404.

    A 404 is ``(None, 404, None)`` — the asset is absent, not a failure.
    Any other miss is a note and a ``None`` status or the HTTP status.
    """
    try:
        resp = session.get(url, timeout=60)
    except Exception as exc:
        return None, None, f"{url} request failed: {exc}"
    if resp.status_code == 404:
        return None, 404, None
    if resp.status_code != 200:
        return None, resp.status_code, f"{url} returned HTTP {resp.status_code}"
    try:
        return resp.json(), 200, None
    except ValueError as exc:
        return None, 200, f"{url} is not JSON: {exc}"


def _release_counts(
    session: Any,
    manifest_url: str,
    legacy_url: str,
    *,
    index: str | os.PathLike | None = None,
) -> tuple[int | None, int | None, list[str], bool]:
    """``(manifest, legacy, notes, release_absent)``.

    ``release_absent`` is true only when both the manifest and the legacy
    asset answer 404. Any other miss leaves it false.
    """
    notes: list[str] = []
    body, status, note = _optional_get_json(session, manifest_url)
    if note:
        notes.append(note)
    if status == 200:
        if not isinstance(body, dict):
            notes.append("thumbnail manifest is not a JSON object")
            return None, None, notes, False
        if "previews" not in body:
            notes.append("thumbnail manifest has no previews count")
            return None, None, notes, False
        count = _nonneg_int(body.get("previews"))
        if count is None:
            notes.append(
                f"thumbnail manifest previews {body.get('previews')!r} is not a "
                "non-negative integer"
            )
        return count, None, notes, False
    legacy_count, legacy_note = _legacy_count(session, legacy_url, index=index)
    if legacy_note:
        notes.append(legacy_note)
    # A confirmed 404 on the manifest and a confirmed 404 on the legacy asset
    # (no note, no count). An error reading either one is not absence.
    release_absent = status == 404 and legacy_count is None and legacy_note is None
    return None, legacy_count, notes, release_absent


def _legacy_count(
    session: Any, url: str, *, index: str | os.PathLike | None = None
) -> tuple[int | None, str | None]:
    try:
        head = session.head(url, allow_redirects=True, timeout=60)
    except Exception as exc:
        return None, f"legacy sidecar HEAD failed: {exc}"
    if head.status_code == 404:
        return None, None
    if head.status_code != 200:
        return None, f"legacy sidecar HEAD returned HTTP {head.status_code}"
    from .download import download_url  # noqa: PLC0415

    try:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / CATALOG_THUMBS_ASSET
            download_url(url, dest, overwrite=True, session=session)
            count = _count_previews(dest, index)
    except Exception as exc:
        return None, f"legacy sidecar could not be counted: {exc}"
    if count is None:
        return None, "legacy sidecar is not a thumbnail database"
    return count, None


def _healthz_count(session: Any, url: str) -> tuple[int | None, str | None]:
    body, status, note = _optional_get_json(session, url)
    if note:
        return None, note
    if status != 200:
        return None, f"{url} returned HTTP {status}"
    count = preview_count_from_health(body)
    if count is None:
        return None, "/healthz did not report thumbnails.count"
    return count, None
