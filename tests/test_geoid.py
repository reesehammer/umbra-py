"""Offline tests for auto-fetching a global geoid grid (``umbra_py.geoid``).

The URL and cache-dir logic is pure standard library and tested with no network.
The fetch is exercised through an *injected* ``download`` callable that writes a
stub file, so the download-and-cache behaviour is covered without touching the
PROJ CDN -- the same discipline :mod:`umbra_py.dem` holds. Unlike a DEM the EGM
geoid grid is a single global file, so there is no tile math to cover.
"""

from __future__ import annotations

from umbra_py import geoid

# --------------------------------------------------------------------------- #
# Pure URL / cache-dir logic (no network, no optional extras).
# --------------------------------------------------------------------------- #


def test_geoid_grid_url_defaults_to_egm96_on_proj_cdn():
    assert geoid.geoid_grid_url() == f"{geoid.PROJ_CDN_BASE}/{geoid.DEFAULT_GEOID_GRID}"
    assert geoid.DEFAULT_GEOID_GRID == "us_nga_egm96_15.tif"


def test_geoid_grid_url_honours_name_and_base():
    url = geoid.geoid_grid_url("us_nga_egm08_25.tif", base="https://x/")
    assert url == "https://x/us_nga_egm08_25.tif"


def test_default_geoid_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("UMBRA_GEOID_DIR", str(tmp_path / "g"))
    assert geoid.default_geoid_cache_dir() == tmp_path / "g"
    monkeypatch.delenv("UMBRA_GEOID_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert geoid.default_geoid_cache_dir() == tmp_path / "cache" / "umbra-py" / "geoid"


# --------------------------------------------------------------------------- #
# Fetch behaviour with an injected downloader (no network).
# --------------------------------------------------------------------------- #


def test_fetch_geoid_grid_downloads_default_to_cache(tmp_path):
    calls = []

    def fake_download(url, dest, *, session=None):
        from pathlib import Path

        calls.append(url)
        dest = Path(dest)
        dest.write_bytes(b"stub-geoid")
        return dest

    out = geoid.fetch_geoid_grid(tmp_path, download=fake_download)
    assert out == tmp_path / geoid.DEFAULT_GEOID_GRID
    assert out.read_bytes() == b"stub-geoid"
    assert calls == [f"{geoid.PROJ_CDN_BASE}/{geoid.DEFAULT_GEOID_GRID}"]


def test_fetch_geoid_grid_honours_name(tmp_path):
    def fake_download(url, dest, *, session=None):
        from pathlib import Path

        dest = Path(dest)
        dest.write_bytes(b"stub")
        return dest

    out = geoid.fetch_geoid_grid(tmp_path, name="us_nga_egm08_25.tif", download=fake_download)
    assert out == tmp_path / "us_nga_egm08_25.tif"
