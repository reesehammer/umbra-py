"""Download Umbra data assets over anonymous HTTPS, with resume support."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

import requests

from ._http import DEFAULT_TIMEOUT, default_session
from .exceptions import DownloadError
from .models import UmbraItem

ProgressCallback = Callable[[int, int | None], None]
_CHUNK = 1 << 20  # 1 MiB


def _filename_from_url(url: str) -> str:
    name = url.split("?")[0].rstrip("/").split("/")[-1]
    return name or "download"


def download_url(
    url: str,
    dest: str | os.PathLike,
    *,
    overwrite: bool = False,
    resume: bool = True,
    session: requests.Session | None = None,
    progress: ProgressCallback | None = None,
) -> Path:
    """Stream ``url`` to ``dest``.

    ``dest`` may be a directory (the server filename is used) or a full file
    path. Partial downloads are written to a ``.part`` sidecar and resumed via
    an HTTP ``Range`` request when ``resume`` is true.
    """
    sess = session or default_session()
    dest = Path(dest)
    if dest.is_dir() or str(dest).endswith(os.sep):
        dest = dest / _filename_from_url(url)
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

    downloaded = existing
    mode = "ab" if append else "wb"
    try:
        with open(part, mode) as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                fh.write(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    progress(downloaded, total)
    except requests.RequestException as exc:
        # A dropped connection mid-body. The bytes fetched so far are flushed to
        # the .part, so leave it in place for a later resume rather than losing
        # the progress.
        raise DownloadError(f"Interrupted download of {url!r}: {exc}") from exc

    # Guard against a silently truncated body (a proxy or server that closes the
    # connection cleanly mid-stream, so no exception is raised). Leave the .part
    # in place so a later call can resume it rather than discarding the bytes
    # already fetched.
    if total is not None and downloaded != total:
        raise DownloadError(
            f"Incomplete download of {url!r}: got {downloaded} bytes, expected {total}"
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
    """Download a single named asset (e.g. ``"GEC"``) of an item."""
    url = item.asset_href(asset)
    return download_url(url, Path(dest_dir), **kwargs)


def download_item(
    item: UmbraItem,
    dest_dir: str | os.PathLike = ".",
    assets: Iterable[str] | None = None,
    **kwargs,
) -> list[Path]:
    """Download several assets of an item.

    Defaults to every product asset present on the item.
    """
    names = list(assets) if assets is not None else item.available_assets
    return [download_asset(item, name, dest_dir, **kwargs) for name in names]
