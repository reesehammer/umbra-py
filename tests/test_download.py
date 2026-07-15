import pytest
import responses

from umbra_py.download import _filename_from_url, download_url
from umbra_py.exceptions import DownloadError


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


@responses.activate
def test_download_interrupted_body_raises_and_keeps_part(tmp_path):
    # Server advertises more bytes than it delivers and the connection breaks
    # mid-stream. The download must fail loudly, not rename a truncated file into
    # place, and it must keep the .part (with its validator) so a later call can
    # resume the bytes already fetched.
    url = "http://example.com/scene.tif"
    body = b"only-half"
    responses.add(
        responses.GET,
        url,
        body=body,
        status=200,
        headers={"Content-Length": str(len(body) + 100), "ETag": '"abc123"'},
    )

    dest = tmp_path / "scene.tif"
    with pytest.raises(DownloadError, match="download"):
        download_url(url, dest)

    # No truncated file is renamed into place, and the validator is kept so a
    # later call can resume against the same object.
    assert not dest.exists()
    part = dest.with_suffix(dest.suffix + ".part")
    assert part.exists()
    assert part.with_suffix(part.suffix + ".etag").read_text() == '"abc123"'


def test_download_verifies_content_length_on_clean_close(tmp_path):
    # A clean early close (no exception) that delivers fewer bytes than
    # Content-Length promised must still be rejected — the post-stream size check
    # is the only thing that catches it.
    url = "http://example.com/scene.tif"

    class _ShortResponse:
        status_code = 200
        headers = {"Content-Length": "100", "ETag": '"abc123"'}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"only-half"  # 9 bytes, well under the promised 100

    class _FakeSession:
        def get(self, *args, **kwargs):
            return _ShortResponse()

    dest = tmp_path / "scene.tif"
    with pytest.raises(DownloadError, match="Incomplete download"):
        download_url(url, dest, session=_FakeSession())

    assert not dest.exists()
    assert dest.with_suffix(dest.suffix + ".part").read_bytes() == b"only-half"


@responses.activate
def test_download_resume_sends_if_range(tmp_path):
    url = "http://example.com/scene.tif"
    dest = tmp_path / "scene.tif"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"partpart")  # 8 bytes already fetched
    part.with_suffix(part.suffix + ".etag").write_text('"v1-etag"')

    responses.add(
        responses.GET,
        url,
        body=b"rest",
        status=206,
        headers={"Content-Length": "4"},
    )

    out = download_url(url, dest)

    assert out.read_bytes() == b"partpartrest"
    req = responses.calls[0].request
    assert req.headers["Range"] == "bytes=8-"
    assert req.headers["If-Range"] == '"v1-etag"'
    assert not part.exists()
    assert not part.with_suffix(part.suffix + ".etag").exists()


@responses.activate
def test_download_restarts_when_object_changed(tmp_path):
    # The .part is stale: the remote object changed, so If-Range fails and the
    # server replies 200 with the whole new object. The result must be the new
    # object in full, never a splice of old+new bytes.
    url = "http://example.com/scene.tif"
    dest = tmp_path / "scene.tif"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"stale-old-partial")
    part.with_suffix(part.suffix + ".etag").write_text('"v1"')

    new_body = b"brand-new-full-object"
    responses.add(
        responses.GET,
        url,
        body=new_body,
        status=200,
        headers={"Content-Length": str(len(new_body)), "ETag": '"v2"'},
    )

    out = download_url(url, dest)

    assert out.read_bytes() == new_body
    assert not part.exists()
