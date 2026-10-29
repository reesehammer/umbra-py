"""Catalog snapshot https, sha256, and defensive SQLite checks."""

from __future__ import annotations

import hashlib
import sqlite3
import warnings
from pathlib import Path

import pytest
import responses
from click.testing import CliRunner

from umbra_py.cli import cli
from umbra_py.constants import CATALOG_SHA256_ASSET, INSECURE_INDEX_URL_ENV
from umbra_py.exceptions import IndexRefreshError
from umbra_py.index import (
    _SCHEMA,
    _SCHEMA_VERSION,
    CatalogIndex,
    _enable_sqlite_defensive,
    _index_rows,
    _open_untrusted_snapshot,
    _schema_object_names,
    _validate_snapshot,
    parse_sha256_manifest,
    refresh_from_release,
    require_index_https,
    sha256_manifest_url,
    write_release_manifest,
)
from umbra_py.models import UmbraItem

URL = "https://example.test/releases/download/catalog-index/catalog.db"
MANIFEST = sha256_manifest_url(URL)
PUBLISHED = Path("/tmp/catalog.db")


def _snapshot(tmp_path: Path, n: int = 2) -> bytes:
    path = tmp_path / "built.db"
    with CatalogIndex(path) as idx:
        for i in range(n):
            idx.add(
                UmbraItem.from_dict(
                    {"id": f"id-{i}", "bbox": [0, 0, 1, 1], "properties": {}, "assets": {}},
                    href=f"https://h/{i}.stac.v2.json",
                )
            )
        idx.set_meta("built_at", "2026-10-27")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    return path.read_bytes()


def _serve(body: bytes, *, manifest: bytes | None = None, manifest_status: int = 404) -> None:
    headers = {
        "ETag": '"v1"',
        "Last-Modified": "Mon, 05 Oct 2026 15:09:48 GMT",
        "Content-Length": str(len(body)),
    }
    responses.add(responses.HEAD, URL, status=200, headers=headers)
    responses.add(responses.GET, URL, body=body, status=200, headers=headers)
    if manifest is None:
        responses.add(responses.GET, MANIFEST, status=manifest_status)
    else:
        responses.add(
            responses.GET,
            MANIFEST,
            body=manifest,
            status=manifest_status,
            headers={"Content-Length": str(len(manifest))},
        )


def test_require_index_https_and_the_opt_out(monkeypatch):
    assert (
        require_index_https(" https://example.test/catalog.db ")
        == "https://example.test/catalog.db"
    )
    with pytest.raises(IndexRefreshError, match="https"):
        require_index_https("http://example.test/catalog.db")
    with pytest.raises(IndexRefreshError, match="https"):
        require_index_https("example.test/catalog.db")
    assert (
        require_index_https("http://example.test/catalog.db", insecure=True)
        == "http://example.test/catalog.db"
    )
    monkeypatch.setenv(INSECURE_INDEX_URL_ENV, "yes")
    assert require_index_https("http://mirror.test/catalog.db") == "http://mirror.test/catalog.db"
    monkeypatch.setenv(INSECURE_INDEX_URL_ENV, "0")
    with pytest.raises(IndexRefreshError, match="https"):
        require_index_https("http://mirror.test/catalog.db")


def test_cli_http_index_url_is_refused(tmp_path):
    result = CliRunner().invoke(
        cli,
        [
            "index",
            "fetch",
            "--db",
            str(tmp_path / "catalog.db"),
            "--url",
            "http://example.test/catalog.db",
        ],
    )
    assert result.exit_code != 0
    assert "https" in result.output


def test_sha256_manifest_round_trip(tmp_path):
    db = tmp_path / "catalog.db"
    db.write_bytes(b"abc")
    other = tmp_path / "notes.md"
    other.write_bytes(b"notes")
    dest = tmp_path / CATALOG_SHA256_ASSET
    write_release_manifest(dest, [db, other])
    parsed = parse_sha256_manifest(dest.read_text())
    assert parsed["catalog.db"] == hashlib.sha256(b"abc").hexdigest()
    assert parsed["notes.md"] == hashlib.sha256(b"notes").hexdigest()
    assert sha256_manifest_url(URL) == MANIFEST
    signed = (
        "https://example.test/releases/download/catalog-index/catalog.db"
        "?X-Amz-Signature=abc&token=1#frag"
    )
    assert sha256_manifest_url(signed) == (
        "https://example.test/releases/download/catalog-index/catalog.sha256"
        "?X-Amz-Signature=abc&token=1"
    )
    binary = hashlib.sha256(b"abc").hexdigest() + " *catalog.db\n"
    assert parse_sha256_manifest(binary)["catalog.db"] == hashlib.sha256(b"abc").hexdigest()
    with pytest.raises(IndexRefreshError, match="basename"):
        parse_sha256_manifest(hashlib.sha256(b"abc").hexdigest() + "  ../catalog.db\n")


@pytest.mark.parametrize("status", [403, 404, 410])
@responses.activate
def test_missing_manifest_warns_and_still_fetches(tmp_path, status):
    body = _snapshot(tmp_path)
    _serve(body, manifest_status=status)
    with pytest.warns(UserWarning, match="catalog.sha256"):
        result = refresh_from_release(tmp_path / "catalog.db", url=URL)
    assert result.changed and result.integrity == "unverified" and result.items == 2


@responses.activate
def test_other_manifest_errors_fail_closed(tmp_path):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_snapshot(tmp_path, n=3))
    body = _snapshot(tmp_path / "next", n=4)
    _serve(body, manifest_status=500)
    with pytest.raises(IndexRefreshError, match="sha256 manifest"):
        refresh_from_release(dest, url=URL)
    with CatalogIndex(dest) as idx:
        assert len(idx) == 3


@responses.activate
def test_matching_manifest_is_verified(tmp_path):
    body = _snapshot(tmp_path)
    digest = hashlib.sha256(body).hexdigest()
    _serve(body, manifest=f"{digest}  catalog.db\n".encode(), manifest_status=200)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = refresh_from_release(tmp_path / "catalog.db", url=URL)
    assert result.integrity == "verified" and result.items == 2


@responses.activate
def test_manifest_mismatch_keeps_the_current_index(tmp_path):
    dest = tmp_path / "catalog.db"
    dest.write_bytes(_snapshot(tmp_path, n=3))
    body = _snapshot(tmp_path / "next", n=4)
    wrong = "ab" * 32
    _serve(body, manifest=f"{wrong}  catalog.db\n".encode(), manifest_status=200)
    with pytest.raises(IndexRefreshError, match="sha256 mismatch"):
        refresh_from_release(dest, url=URL)
    assert not (tmp_path / "catalog.db.next").exists()
    with CatalogIndex(dest) as idx:
        assert len(idx) == 3


@responses.activate
def test_cli_if_changed_warns_when_the_manifest_is_missing(tmp_path):
    body = _snapshot(tmp_path)
    _serve(body)
    db = tmp_path / "catalog.db"
    result = CliRunner().invoke(
        cli, ["index", "fetch", "--if-changed", "--db", str(db), "--url", URL]
    )
    assert result.exit_code == 0, result.output
    assert "not integrity-checked" in result.output
    assert "Refreshed index" in result.output


def test_fresh_index_matches_the_schema_allowlist(tmp_path):
    path = tmp_path / "catalog.db"
    path.write_bytes(_snapshot(tmp_path / "src"))
    conn = sqlite3.connect(path)
    try:
        names = {(row[0], row[1]) for row in conn.execute("SELECT type, name FROM sqlite_master")}
    finally:
        conn.close()
    assert names == _schema_object_names(_SCHEMA)
    rows, built = _validate_snapshot(path)
    assert rows == 2 and built == "2026-10-27"


def test_untrusted_open_is_read_only(tmp_path):
    path = tmp_path / "catalog.db"
    path.write_bytes(_snapshot(tmp_path / "src"))
    conn = _open_untrusted_snapshot(path)
    try:
        assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        if hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE) == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope(x)")
    finally:
        conn.close()


def test_defensive_flag_is_skipped_when_the_runtime_lacks_it(monkeypatch):
    class _Conn:
        def __init__(self) -> None:
            self.called = False

        def setconfig(self, flag: object, enabled: bool) -> None:
            self.called = True

    monkeypatch.setattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", 99, raising=False)
    present = _Conn()
    _enable_sqlite_defensive(present)  # type: ignore[arg-type]
    assert present.called
    monkeypatch.delattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE", raising=False)
    absent = _Conn()
    _enable_sqlite_defensive(absent)  # type: ignore[arg-type]
    assert not absent.called


def _with_extra_sql(tmp_path: Path, sql: str) -> Path:
    path = tmp_path / "evil.db"
    path.write_bytes(_snapshot(tmp_path / "src"))
    conn = sqlite3.connect(path)
    conn.execute(sql)
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.close()
    return path


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TRIGGER evil AFTER INSERT ON items BEGIN SELECT 1; END",
        "CREATE VIEW evil AS SELECT 1",
        "CREATE TABLE evil(x)",
    ],
)
def test_unexpected_sqlite_objects_are_rejected(tmp_path, sql):
    path = _with_extra_sql(tmp_path, sql)
    with pytest.raises(IndexRefreshError, match="unexpected sqlite objects"):
        _validate_snapshot(path)


def test_newer_schema_says_upgrade_before_the_allowlist(tmp_path):
    path = _with_extra_sql(tmp_path, "CREATE TABLE evil(x)")
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(IndexRefreshError, match="Upgrade umbra-py"):
        _validate_snapshot(path)


def test_ro_uri_quotes_hash_and_percent(tmp_path):
    folder = tmp_path / "a#b%c"
    folder.mkdir()
    path = folder / "catalog.db"
    path.write_bytes(_snapshot(tmp_path / "src"))
    conn = _open_untrusted_snapshot(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
    finally:
        conn.close()
    assert _index_rows(path) == 2


@pytest.mark.skipif(not PUBLISHED.exists(), reason="published catalog.db was not downloaded")
def test_published_catalog_db_passes_defensive_validation():
    conn = sqlite3.connect(f"file:{PUBLISHED}?mode=ro", uri=True)
    try:
        published = {
            (row[0], row[1]) for row in conn.execute("SELECT type, name FROM sqlite_master")
        }
    finally:
        conn.close()
    assert published == _schema_object_names(_SCHEMA)
    rows, built = _validate_snapshot(PUBLISHED)
    assert rows > 0
    assert built
