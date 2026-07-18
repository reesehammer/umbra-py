import hashlib

import pytest
import responses

from umbra_py.download import _filename_from_url, _single_part_md5, download_url
from umbra_py.exceptions import DownloadError


def test_filename_from_url():
    assert _filename_from_url("http://x/a/b_GEC.tif?foo=1") == "b_GEC.tif"
    assert _filename_from_url("http://x/") == "x"


def test_single_part_md5_recognizes_only_plain_md5_etags():
    md5 = "d41d8cd98f00b204e9800998ecf8427e"
    # A single-part S3 ETag is the hex MD5, quoted; case and the weak-validator
    # prefix are normalized away.
    assert _single_part_md5(f'"{md5}"') == md5
    assert _single_part_md5(f'"{md5.upper()}"') == md5
    assert _single_part_md5(f'W/"{md5}"') == md5
    # A multipart ETag ("<hash>-<n>") is not a plain MD5 — skip it.
    assert _single_part_md5(f'"{md5}-3"') is None
    # Non-MD5 opaque validators and missing headers are skipped.
    assert _single_part_md5('"v1-etag"') is None
    assert _single_part_md5("") is None
    assert _single_part_md5(None) is None


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
def test_download_verifies_matching_md5_etag(tmp_path):
    # A single-part S3 ETag is the object's hex MD5. When the delivered bytes
    # hash to it, the download completes normally.
    url = "http://example.com/scene.tif"
    body = b"correct-sar-bytes" * 50
    etag = f'"{hashlib.md5(body).hexdigest()}"'
    responses.add(
        responses.GET,
        url,
        body=body,
        status=200,
        headers={"Content-Length": str(len(body)), "ETag": etag},
    )

    out = download_url(url, tmp_path / "scene.tif")
    assert out.read_bytes() == body
    assert not out.with_suffix(out.suffix + ".part").exists()


@responses.activate
def test_download_rejects_body_that_fails_md5_etag(tmp_path):
    # The byte count is correct but the content was corrupted in transit, so it
    # does not hash to the ETag's MD5. This must fail loudly and discard the
    # full-but-corrupt .part (a resume can't repair complete-length wrong bytes).
    url = "http://example.com/scene.tif"
    body = b"corrupted-on-the-wire"
    wrong_md5 = hashlib.md5(b"the-original-object").hexdigest()
    responses.add(
        responses.GET,
        url,
        body=body,
        status=200,
        headers={"Content-Length": str(len(body)), "ETag": f'"{wrong_md5}"'},
    )

    dest = tmp_path / "scene.tif"
    with pytest.raises(DownloadError, match="Checksum mismatch"):
        download_url(url, dest)

    assert not dest.exists()
    part = dest.with_suffix(dest.suffix + ".part")
    assert not part.exists()
    assert not part.with_suffix(part.suffix + ".etag").exists()


@responses.activate
def test_download_verify_false_skips_checksum(tmp_path):
    # With verify=False a mismatched MD5 ETag is ignored (opt-out for callers
    # that don't want the extra read of a multi-GB file).
    url = "http://example.com/scene.tif"
    body = b"corrupted-on-the-wire"
    wrong_md5 = hashlib.md5(b"something-else").hexdigest()
    responses.add(
        responses.GET,
        url,
        body=body,
        status=200,
        headers={"Content-Length": str(len(body)), "ETag": f'"{wrong_md5}"'},
    )

    out = download_url(url, tmp_path / "scene.tif", verify=False)
    assert out.read_bytes() == body


@responses.activate
def test_download_skips_verification_for_multipart_etag(tmp_path):
    # A multipart ETag ("<hash>-<n>") is not a plain MD5 of the bytes, so the
    # content check is skipped rather than raising a spurious mismatch.
    url = "http://example.com/scene.tif"
    body = b"multipart-object-bytes"
    responses.add(
        responses.GET,
        url,
        body=body,
        status=200,
        headers={"Content-Length": str(len(body)), "ETag": '"abc123def456abc123def456abc12345-7"'},
    )

    out = download_url(url, tmp_path / "scene.tif")
    assert out.read_bytes() == body


@responses.activate
def test_download_resume_verifies_whole_object_md5(tmp_path):
    # After a resumed append, the *whole* finished file (existing partial bytes
    # + appended tail) must hash to the object's MD5 — the ETag identifies the
    # complete object, not the delivered range.
    url = "http://example.com/scene.tif"
    dest = tmp_path / "scene.tif"
    part = dest.with_suffix(dest.suffix + ".part")
    full = b"partpartrest"
    etag = f'"{hashlib.md5(full).hexdigest()}"'
    part.write_bytes(b"partpart")  # 8 bytes already fetched
    part.with_suffix(part.suffix + ".etag").write_text(etag)

    responses.add(
        responses.GET,
        url,
        body=b"rest",
        status=206,
        headers={"Content-Length": "4", "ETag": etag},
    )

    out = download_url(url, dest)
    assert out.read_bytes() == full
    assert not part.exists()


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
