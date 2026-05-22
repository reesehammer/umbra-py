import responses

from umbra_py.download import _filename_from_url, download_url


def test_filename_from_url():
    assert _filename_from_url("http://x/a/b_GEC.tif?foo=1") == "b_GEC.tif"
    assert _filename_from_url("http://x/") == "x"


@responses.activate
def test_download_to_directory(tmp_path):
    url = "http://example.com/data/scene_GEC.tif"
    body = b"hello-sar" * 100
    responses.add(
        responses.GET, url, body=body, status=200, headers={"Content-Length": str(len(body))}
    )

    out = download_url(url, tmp_path)
    assert out.name == "scene_GEC.tif"
    assert out.read_bytes() == body
    assert not out.with_suffix(out.suffix + ".part").exists()


@responses.activate
def test_download_skips_existing(tmp_path):
    url = "http://example.com/scene.tif"
    dest = tmp_path / "scene.tif"
    dest.write_bytes(b"already here")
    # No responses registered: if it tried to fetch, it would error.
    out = download_url(url, dest)
    assert out.read_bytes() == b"already here"


@responses.activate
def test_download_progress_callback(tmp_path):
    url = "http://example.com/scene.tif"
    body = b"x" * 2048
    responses.add(
        responses.GET, url, body=body, status=200, headers={"Content-Length": str(len(body))}
    )

    seen = []
    download_url(url, tmp_path / "scene.tif", progress=lambda d, t: seen.append((d, t)))
    assert seen[-1][0] == len(body)
    assert seen[-1][1] == len(body)
