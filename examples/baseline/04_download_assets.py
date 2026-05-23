"""Download an Umbra asset anonymously, two different ways.

Why this is here
----------------
The Umbra bucket is open to the public, so no AWS credentials are needed —
but every downloader still has to:

1. Tell its HTTP / S3 client to skip credential lookup (anonymous mode).
2. Stream the response (some assets are tens of GBs).
3. Resume on failure. Multi-GB downloads over residential or hotel WiFi
   really do drop, and re-downloading 12 GB from scratch is not fun.

This script shows the two common paths people end up writing:

* ``download_via_https()`` — streams the asset over HTTPS with ``requests``,
  uses a ``.part`` sidecar, and resumes via the HTTP ``Range`` header.
* ``download_via_s3()`` — uses ``boto3`` configured with
  ``botocore.UNSIGNED`` to read the same object directly from the
  ``umbra-open-data-catalog`` S3 bucket.

Both approaches are roughly 30-50 lines of plumbing per project. umbra-py's
``download_asset`` / ``download_item`` collapses this to one call.

Requires::

    pip install requests boto3 botocore

Run::

    python 04_download_assets.py
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests
from botocore import UNSIGNED
from botocore.config import Config

# An example public GEC GeoTIFF. In practice you get the URL by walking the
# catalog (see 02_search_catalog_handrolled.py) and resolving the GEC asset
# (see 03_find_the_geotiff.py).
EXAMPLE_HTTPS_URL = (
    "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/"
    "stac/2024/2024-02/2024-02-08/<acquisition-id>/<filename>_GEC.tif"
)
EXAMPLE_S3_BUCKET = "umbra-open-data-catalog"
EXAMPLE_S3_KEY = "stac/2024/2024-02/2024-02-08/<acquisition-id>/<filename>_GEC.tif"

CHUNK = 1 << 20  # 1 MiB


def download_via_https(
    url: str,
    dest: Path,
    *,
    resume: bool = True,
    overwrite: bool = False,
) -> Path:
    """Stream ``url`` to ``dest`` with resume via HTTP Range."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not overwrite:
        return dest

    part = dest.with_suffix(dest.suffix + ".part")
    existing = part.stat().st_size if (resume and part.exists()) else 0
    headers = {"Range": f"bytes={existing}-"} if existing else {}

    with requests.get(url, stream=True, headers=headers, timeout=60) as resp:
        resp.raise_for_status()

        # If we asked for a range and the server replied with a full 200,
        # it ignored us — start over.
        appending = existing > 0 and resp.status_code == 206
        if existing and not appending:
            existing = 0

        mode = "ab" if appending else "wb"
        with open(part, mode) as fh:
            for chunk in resp.iter_content(chunk_size=CHUNK):
                if chunk:
                    fh.write(chunk)

    part.replace(dest)
    return dest


def download_via_s3(bucket: str, key: str, dest: Path) -> Path:
    """Stream an S3 object without signing the request (anonymous access)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3 = boto3.client(
        "s3",
        region_name="us-west-2",
        config=Config(signature_version=UNSIGNED),
    )
    # ``download_file`` handles multipart and retries; for finer-grained
    # control (resume, progress) drop to ``get_object`` + ``StreamingBody``.
    s3.download_file(bucket, key, str(dest))
    return dest


def _filename_from_url(url: str) -> str:
    return os.path.basename(urlparse(url).path) or "download.bin"


def main() -> None:
    out_dir = Path("downloads")

    print(f"HTTPS download would write to: {out_dir / _filename_from_url(EXAMPLE_HTTPS_URL)}")
    print(f"S3    download would write to: {out_dir / Path(EXAMPLE_S3_KEY).name}")
    print()
    print("Replace EXAMPLE_HTTPS_URL / EXAMPLE_S3_KEY with a real asset to run.")
    print("See 02_search_catalog_handrolled.py + 03_find_the_geotiff.py to find one.")


if __name__ == "__main__":
    main()
