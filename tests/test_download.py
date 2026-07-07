import responses

from umbra_py.download import _filename_from_url, download_asset, download_url
from umbra_py.models import UmbraItem


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
def test_download_asset_into_nonexistent_dir_does_not_collide(tmp_path):
    """download_asset must treat dest_dir as a directory even when it doesn't
    exist yet. Regression: a not-yet-created dest_dir was treated as a file
    path, so two assets of one item both wrote to a single file named after
    the directory -- silently returning the same file for both (which made
    `umbra ccd` compare a scene with itself)."""

    def item_for(name: str) -> UmbraItem:
        href = f"https://s3.example.com/tasks/T/{name}/{name}.stac.v2.json"
        asset = f"{name}_SICD_MM.nitf"
        url = f"https://s3.example.com/tasks/T/{name}/{name}_SICD.nitf"
        responses.add(responses.GET, url, body=(name.encode() * 50), status=200)
        return UmbraItem.from_dict(
            {"id": name, "assets": {asset: {"href": "", "type": "application/vnd.nitf"}}},
            href=href,
        )

    dest = tmp_path / "cache" / "sicd"  # neither segment exists yet
    a = download_asset(item_for("2025-01-28-05-26-27_UMBRA-10"), "SICD", dest)
    b = download_asset(item_for("2025-03-19-05-28-46_UMBRA-09"), "SICD", dest)

    assert a != b
    assert a.parent == dest and dest.is_dir()
    assert a.read_bytes() != b.read_bytes()


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
