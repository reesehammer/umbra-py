"""Download Umbra data assets over anonymous HTTPS, with resume support."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from ._http import DEFAULT_TIMEOUT, default_session
from .constants import MCP_DOWNLOAD_ROOT_ENV
from .exceptions import DownloadError
from .models import UmbraItem

ProgressCallback = Callable[[int, int | None], None]
_CHUNK = 1 << 20  # 1 MiB


def _filename_from_url(url: str) -> str:
    """Basename of ``url``, percent-decoded once, or ``download`` if it has none.

    Query and fragment are stripped first. ``/`` and ``\\`` are separators, so
    only the last segment is kept. That segment is decoded once (``%252e``
    stays ``%2e``). A leading dot, a control character, or a slash or
    backslash that survived the decode is refused — those are how an href
    names a hidden file or a path instead of a filename.
    """
    cut = url.split("#", 1)[0].split("?", 1)[0]
    path = urlsplit(cut).path if "://" in cut else cut
    segment = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    name = unquote(segment) or "download"
    _reject_download_filename(name)
    return name


def _reject_download_filename(name: str) -> None:
    if (
        not name
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        raise DownloadError(f"Refusing unsafe download filename {name!r}.")


def _ensure_inside(root: Path, candidate: Path) -> None:
    """Raise when ``candidate`` does not resolve inside ``root``.

    ``resolve`` follows a symlink, so a name that points at a file outside
    ``root`` is refused before anything is written or returned.
    """
    root_resolved = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise DownloadError(
            f"Refusing to write {resolved}; it is outside {root_resolved}."
        ) from None


def mcp_download_root() -> Path:
    """Directory local MCP ``download_asset(confirm=true)`` may write under.

    ``UMBRA_MCP_DOWNLOAD_ROOT`` when set, otherwise the process working directory.
    """
    raw = os.environ.get(MCP_DOWNLOAD_ROOT_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def confine_download_dir(dest_dir: str | os.PathLike, *, root: Path | None = None) -> Path:
    """Resolve ``dest_dir`` and require it to stay inside ``root``.

    Relative paths are resolved against ``root`` (not the working directory,
    unless that is the root). An absolute path must already be inside it.
    """
    base = root.resolve() if root is not None else mcp_download_root()
    raw = Path(dest_dir).expanduser()
    candidate = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise DownloadError(
            f"dest_dir {os.fspath(dest_dir)!r} is outside the download root {base}. "
            f"Set {MCP_DOWNLOAD_ROOT_ENV} to allow that directory."
        ) from None
    return candidate


def _single_part_md5(etag: str | None) -> str | None:
    """Return the hex MD5 an S3 ETag encodes, or ``None`` if it is not one.

    For a single-part upload S3's ETag is the hex MD5 of the object wrapped in
    quotes (optionally a weak-validator ``W/`` prefix). A *multipart* upload's
    ETag is ``"<hash>-<partcount>"`` — not a plain MD5 of the bytes — so it is
    skipped rather than compared against a wrong value.
    """
    if not etag:
        return None
    value = etag.strip()
    if value.startswith("W/"):
        value = value[2:]
    value = value.strip('"').lower()
    if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _file_md5(path: Path) -> str:
    """Stream ``path`` through MD5 without holding it in memory."""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_url(
    url: str,
    dest: str | os.PathLike,
    *,
    overwrite: bool = False,
    resume: bool = True,
    verify: bool = True,
    session: requests.Session | None = None,
    progress: ProgressCallback | None = None,
    max_bytes: int | None = None,
) -> Path:
    """Stream ``url`` to ``dest``.

    ``dest`` may be a directory (the filename is the URL's basename; see
    :func:`_filename_from_url`) or a full file path. A trailing path separator
    means a directory even when it does not exist yet. The file is refused
    when that basename is unsafe or the resolved path leaves the directory.
    Partial downloads are written to a ``.part`` sidecar and resumed via an
    HTTP ``Range`` request when ``resume`` is true.

    When ``verify`` is true (the default) and the server exposes a single-part
    S3 ``ETag`` (the object's hex MD5), the finished file is hashed and compared
    against it, so on-the-wire corruption a correct byte count can't catch fails
    loudly. Multipart ETags (``"<hash>-<n>"``) are not a plain MD5 and are
    skipped.

    ``max_bytes``, when set, refuses a transfer larger than that many bytes
    (Content-Length when the server sends one, and the bytes actually read
    either way). An existing finished file is still returned when
    ``overwrite`` is false; the cap applies to a transfer.
    """
    if max_bytes is not None and max_bytes < 0:
        raise DownloadError("max_bytes must be zero or a positive number of bytes.")
    sess = session or default_session()
    raw_dest = os.fspath(dest)
    dest = Path(dest)
    if dest.is_dir() or raw_dest.endswith(("/", os.sep)):
        root = dest
        dest = root / _filename_from_url(url)
        _ensure_inside(root, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not overwrite:
        return dest

    part = dest.with_suffix(dest.suffix + ".part")
    # The remote validator (S3 ETag) of the in-progress object, stashed next to
    # the .part so a resumed request can prove — via If-Range — that it is still
    # appending to the *same* object. Without this, a Range resume against a
    # changed object silently splices two different files together.
    etag_path = part.with_suffix(part.suffix + ".etag")
    existing = part.stat().st_size if (resume and part.exists()) else 0

    headers = {}
    stored_etag = ""
    if existing:
        headers["Range"] = f"bytes={existing}-"
        stored_etag = etag_path.read_text().strip() if etag_path.exists() else ""
        if stored_etag:
            headers["If-Range"] = stored_etag
    try:
        resp = sess.get(url, stream=True, headers=headers, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError(f"Failed to download {url!r}: {exc}") from exc

    # A 206 means the server honored our Range (If-Range matched); anything else
    # (a 200 because the object changed or the server ignored Range) restarts
    # from the beginning.
    append = existing > 0 and resp.status_code == 206
    if existing and not append:
        existing = 0

    # The whole-object validator, used below to verify the finished file. It is
    # present on both 200 and 206 responses; on a resume where the server omits
    # the header, the stored ETag (which If-Range just confirmed still matches)
    # identifies the same object.
    object_etag = resp.headers.get("ETag") or (stored_etag if append else "")

    # Persist the validator for a future resume. On a 200 the ETag describes the
    # whole object we're about to write; on a 206 we keep the one we already
    # sent (still valid).
    if not append:
        etag = resp.headers.get("ETag")
        if etag:
            etag_path.write_text(etag)
        elif etag_path.exists():
            etag_path.unlink()

    total: int | None = None
    if "Content-Length" in resp.headers:
        total = int(resp.headers["Content-Length"]) + (existing if append else 0)

    if max_bytes is not None and (
        existing > max_bytes or (total is not None and total > max_bytes)
    ):
        part.unlink(missing_ok=True)
        etag_path.unlink(missing_ok=True)
        raise DownloadError(
            f"Download of {url!r} exceeds max_bytes ({max_bytes}); "
            f"server reported {total if total is not None else existing} bytes."
        )

    downloaded = existing
    mode = "ab" if append else "wb"
    over_cap = False
    try:
        with open(part, mode) as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                if max_bytes is not None and downloaded > max_bytes:
                    over_cap = True
                    break
                if progress is not None:
                    progress(downloaded, total)
    except requests.RequestException as exc:
        # A dropped connection mid-body. The bytes fetched so far are flushed to
        # the .part, so leave it in place for a later resume rather than losing
        # the progress.
        raise DownloadError(f"Interrupted download of {url!r}: {exc}") from exc
    if over_cap:
        # A resume cannot shrink a body that is already over the cap.
        part.unlink(missing_ok=True)
        etag_path.unlink(missing_ok=True)
        raise DownloadError(f"Download of {url!r} exceeded max_bytes ({max_bytes}).")

    # Guard against a silently truncated body (a proxy or server that closes the
    # connection cleanly mid-stream, so no exception is raised). Leave the .part
    # in place so a later call can resume it rather than discarding the bytes
    # already fetched.
    if total is not None and downloaded != total:
        raise DownloadError(
            f"Incomplete download of {url!r}: got {downloaded} bytes, expected {total}"
        )

    # Content verification. A correct byte count still passes a body that was
    # corrupted in transit; the object's MD5 catches that. Only a single-part S3
    # ETag is a plain MD5 we can reproduce (multipart ETags are skipped). A
    # checksum mismatch means the complete-length bytes are wrong, so resuming
    # can't repair them — discard the .part (and its validator) so a retry
    # re-downloads cleanly instead of "resuming" a full-but-corrupt file.
    if verify:
        expected_md5 = _single_part_md5(object_etag)
        if expected_md5 is not None:
            actual_md5 = _file_md5(part)
            if actual_md5 != expected_md5:
                part.unlink(missing_ok=True)
                etag_path.unlink(missing_ok=True)
                raise DownloadError(
                    f"Checksum mismatch for {url!r}: expected MD5 {expected_md5}, got {actual_md5}"
                )

    part.replace(dest)
    if etag_path.exists():
        etag_path.unlink()
    return dest


def download_asset(
    item: UmbraItem,
    asset: str,
    dest_dir: str | os.PathLike = ".",
    **kwargs,
) -> Path:
    """Download one asset of an item.

    ``asset`` is a product type in any case (``"GEC"``), a schema v2 key
    (``"gec"``, ``"gec_2"``, ``"csi_sidd"``, ``"file_tif"``), or the original
    filename (``umbra-py:filename``).
    """
    url = item.asset_href(asset)
    return download_url(url, Path(dest_dir), **kwargs)


def download_item(
    item: UmbraItem,
    dest_dir: str | os.PathLike = ".",
    assets: Iterable[str] | None = None,
    **kwargs,
) -> list[Path]:
    """Download several assets of an item.

    Each name is resolved like :meth:`UmbraItem.asset_href`: a product type
    in any case, a schema v2 key (``gec``, ``gec_2``, ``file_tif``), or the
    original filename. Defaults to every product type present (one primary
    file each).
    """
    names = list(assets) if assets is not None else item.available_assets
    return [download_asset(item, name, dest_dir, **kwargs) for name in names]
