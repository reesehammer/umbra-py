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
    existing = part.stat().st_size if (resume and part.exists()) else 0

    headers = {"Range": f"bytes={existing}-"} if existing else {}
    try:
        resp = sess.get(url, stream=True, headers=headers, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise DownloadError(f"Failed to download {url!r}: {exc}") from exc

    # If the server ignored our Range request, restart from the beginning.
    append = existing > 0 and resp.status_code == 206
    if existing and not append:
        existing = 0

    total: int | None = None
    if "Content-Length" in resp.headers:
        total = int(resp.headers["Content-Length"]) + (existing if append else 0)

    downloaded = existing
    mode = "ab" if append else "wb"
    with open(part, mode) as fh:
        for chunk in resp.iter_content(chunk_size=_CHUNK):
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            if progress is not None:
                progress(downloaded, total)

    part.replace(dest)
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
