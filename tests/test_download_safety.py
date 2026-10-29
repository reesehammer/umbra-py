"""Filename sanitizing, destination confinement, and ``max_bytes``."""

from __future__ import annotations

import os

import pytest
import responses

from umbra_py.constants import MCP_DOWNLOAD_ROOT_ENV
from umbra_py.download import (
    _filename_from_url,
    confine_download_dir,
    download_url,
    mcp_download_root,
)
from umbra_py.exceptions import DownloadError


def test_filename_takes_the_basename_and_decodes_once():
    assert _filename_from_url("https://h/a/../../etc/passwd") == "passwd"
    assert _filename_from_url("https://h/dir/scene.tif?x=1#frag") == "scene.tif"
    # One decode: %252e is a literal %2e, not a dot.
    assert _filename_from_url("https://h/%252e%252e") == "%2e%2e"
    # Backslash is a separator, so only the last segment is kept.
    assert _filename_from_url("https://h/a\\..\\..\\x") == "x"


@pytest.mark.parametrize(
    "url",
    [
        "https://h/%2eenv",
        "https://h/%2e%2e",
        "https://h/foo%2Fbar",
        "https://h/foo%5Cbar",
        "https://h/bad%0aname",
        "https://h/bad%7fname",
    ],
)
def test_filename_rejects_dot_slash_backslash_and_controls(url):
    with pytest.raises(DownloadError, match="unsafe download filename"):
        _filename_from_url(url)


@responses.activate
def test_download_to_a_new_directory_stays_inside_it(tmp_path):
    url = "http://example.com/data/scene_GEC.tif"
    body = b"hello-sar"
    responses.add(
        responses.GET, url, body=body, status=200, headers={"Content-Length": str(len(body))}
    )
    dest = str(tmp_path / "new") + os.sep
    out = download_url(url, dest)
    assert out == tmp_path / "new" / "scene_GEC.tif"
    assert out.read_bytes() == body


@responses.activate
def test_download_refuses_a_symlink_that_leaves_the_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "scene.tif"
    secret.write_bytes(b"keep")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "scene.tif").symlink_to(secret)
    url = "http://example.com/scene.tif"
    responses.add(responses.GET, url, body=b"pwned", status=200, headers={"Content-Length": "5"})
    with pytest.raises(DownloadError, match="outside"):
        download_url(url, dest)
    assert secret.read_bytes() == b"keep"


@responses.activate
def test_max_bytes_refuses_a_long_content_length(tmp_path):
    url = "http://example.com/scene.tif"
    body = b"0123456789"
    responses.add(
        responses.GET, url, body=body, status=200, headers={"Content-Length": str(len(body))}
    )
    dest = tmp_path / "scene.tif"
    with pytest.raises(DownloadError, match="max_bytes"):
        download_url(url, dest, max_bytes=4)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


@responses.activate
def test_max_bytes_stops_a_body_with_no_content_length(tmp_path):
    """A missing Content-Length still stops once the bytes read pass the cap."""
    url = "http://example.com/scene.tif"
    responses.add(responses.GET, url, body=b"0123456789", status=200)
    dest = tmp_path / "scene.tif"
    with pytest.raises(DownloadError, match="exceeded max_bytes"):
        download_url(url, dest, max_bytes=6)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_help_shows_max_bytes():
    from click.testing import CliRunner

    from umbra_py.cli import cli

    result = CliRunner().invoke(cli, ["download", "--help"])
    assert result.exit_code == 0
    assert "--max-bytes" in result.output
    assert "--dest" in result.output


def test_confine_download_dir_defaults_to_cwd_and_rejects_escapes(tmp_path, monkeypatch):
    monkeypatch.delenv(MCP_DOWNLOAD_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    assert mcp_download_root() == tmp_path.resolve()
    assert confine_download_dir("nested") == (tmp_path / "nested").resolve()
    assert confine_download_dir(".") == tmp_path.resolve()
    with pytest.raises(DownloadError, match="outside"):
        confine_download_dir("..")
    with pytest.raises(DownloadError, match="outside"):
        confine_download_dir(tmp_path.parent)

    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(MCP_DOWNLOAD_ROOT_ENV, str(root))
    assert confine_download_dir("child") == (root / "child").resolve()
    assert confine_download_dir(root / "child") == (root / "child").resolve()
    with pytest.raises(DownloadError, match="UMBRA_MCP_DOWNLOAD_ROOT"):
        confine_download_dir(tmp_path / "elsewhere")
