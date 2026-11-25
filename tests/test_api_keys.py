"""Optional API keys on ``umbra serve --public``.

Anonymous traffic keeps the per-IP bucket. A valid key gets a higher burst
and a higher sustained rate. These tests never call the live host.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from umbra_py import serve
from umbra_py.api_keys import ApiKeyStore, Signup, SignupError, keys_path_for, signup_from_mapping
from umbra_py.cli import cli
from umbra_py.cli._root import main
from umbra_py.exceptions import UmbraError

from .test_public_limits import _client, _index, pytestmark_public_app, pytestmark_serve

REPO = Path(__file__).resolve().parents[1]


def _fast_search(monkeypatch) -> None:
    monkeypatch.setattr(serve, "run_search", lambda source, **kwargs: ([], False))


def test_signup_rejects_a_bad_email_and_ignores_extra_fields(tmp_path):
    with pytest.raises(SignupError):
        signup_from_mapping({"email": "not-an-email"})
    with pytest.raises(SignupError):
        signup_from_mapping({"name": "Ada"})
    signup = signup_from_mapping(
        {
            "email": " Ada@Example.com ",
            "name": "Ada",
            "organization": "Example Lab",
            "intended_use": "short bursts",
            "client_ip": "203.0.113.9",
        }
    )
    assert signup.email == "ada@example.com"
    assert not hasattr(signup, "client_ip")
    path = tmp_path / "api-keys.json"
    store = ApiKeyStore(path, clock=lambda: 1_700_000_000.0)
    secret, record = store.create(signup)
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "203.0.113.9" not in raw
    assert "client_ip" not in raw
    saved = json.loads(raw)["keys"][0]
    assert saved["digest"].startswith("sha256:")
    assert saved["email"] == "ada@example.com"
    assert saved["name"] == "Ada"
    assert set(saved) == {
        "digest",
        "email",
        "name",
        "organization",
        "intended_use",
        "created_at",
        "revoked",
        "last_used",
    }
    listed = store.list_public()
    assert "digest" not in listed[0]
    assert "sha256" not in json.dumps(listed)


def test_pepper_is_hmac_and_a_restart_reloads_the_hash(tmp_path):
    path = tmp_path / "api-keys.json"
    store = ApiKeyStore(path, pepper="spice", clock=lambda: 1_700_000_000.0)
    secret, record = store.create(Signup("ada@example.com", None, None, None))
    assert record.digest.startswith("hmac-sha256:")
    reloaded = ApiKeyStore(path, pepper="spice")
    assert reloaded.authenticate({"authorization": f"Bearer {secret}"}).status == "ok"
    assert ApiKeyStore(path, pepper="other").authenticate({"x-api-key": secret}).status == "invalid"
    bare = ApiKeyStore(path)
    assert bare.authenticate({"authorization": f"Bearer {secret}"}).status == "invalid"


def test_revoke_is_ambiguous_until_created_is_given(tmp_path):
    moments = [1_700_000_000.0, 1_700_086_400.0]

    def clock() -> float:
        return moments.pop(0)

    path = tmp_path / "api-keys.json"
    store = ApiKeyStore(path, clock=clock)
    signup = Signup("ada@example.com", None, None, None)
    _first, earlier = store.create(signup)
    _second, later = store.create(signup)
    with pytest.raises(UmbraError) as excinfo:
        store.revoke("Ada@Example.com")
    assert earlier.created_at in (excinfo.value.hint or "")
    assert later.created_at in (excinfo.value.hint or "")
    assert "sha256" not in str(excinfo.value)
    assert store.authenticate({"authorization": "Bearer nope"}).status == "invalid"
    revoked = store.revoke("ada@example.com", created_at=earlier.created_at)
    assert revoked[0].revoked is True
    still = [row for row in store.list_public() if not row["revoked"]]
    assert len(still) == 1
    assert still[0]["created_at"] == later.created_at


def test_keyed_in_flight_cap_is_higher_than_the_anonymous_cap():
    gate = serve.ClientInFlight(serve.PUBLIC_CLIENT_CONCURRENCY, key_limit=32)
    assert gate.limit_for("ip:203.0.113.9") == 8
    assert gate.limit_for("key:abc") == 32
    assert gate.acquire("ip:203.0.113.9") is True


def test_key_bucket_allows_a_burst_of_64_then_refuses():
    """Frozen clock: 64 tokens, then a 429's worth of wait. No refill mid-burst."""
    limiter = serve.RateLimiter(600, burst=64, time_fn=lambda: 0.0)
    for _ in range(64):
        allowed, _remaining, retry_after = limiter.hit("key:abc")
        assert allowed
        assert retry_after == 0
    allowed, _remaining, retry_after = limiter.hit("key:abc")
    assert allowed is False
    assert retry_after >= 1
    message = limiter.rejection(retry_after, scope="key")["message"]
    assert "600" in message
    assert "burst 64" in message
    assert "per key" in message


@pytestmark_public_app
def test_signup_validates_email_stores_a_hash_and_is_rate_limited(tmp_path, monkeypatch):
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, cache_dir=tmp_path / "art")
    headers = {"x-real-ip": "203.0.113.40"}
    with _client(app) as client:
        form = client.get("/keys", headers=headers)
        assert form.status_code == 200
        assert 'name="email"' in form.text
        bad = client.post("/keys", json={"email": "not-an-email"}, headers=headers)
        assert bad.status_code == 400
        assert not keys_path_for(index).exists()
        created = []
        for i in range(3):
            resp = client.post(
                "/keys",
                json={
                    "email": f"ada{i}@example.com",
                    "name": "Ada",
                    "client_ip": "203.0.113.40",
                },
                headers=headers,
            )
            assert resp.status_code == 201, resp.text
            assert resp.headers["cache-control"] == "no-store"
            created.append(resp.json()["key"])
        blocked = client.post("/keys", json={"email": "ada3@example.com"}, headers=headers)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"]
        assert "Sign-up" in blocked.json()["message"]
        assert blocked.headers["x-ratelimit-limit"] == "5"
    raw = keys_path_for(index).read_text(encoding="utf-8")
    for secret in created:
        assert secret not in raw
    assert "203.0.113.40" not in raw
    assert "client_ip" not in raw
    assert raw.count("sha256:") == 3


@pytestmark_public_app
def test_html_form_shows_the_key_once(tmp_path):
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, cache_dir=tmp_path / "art")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            data={"email": "Ada@Example.com", "organization": "Example Lab"},
            headers={"x-real-ip": "198.51.100.8"},
        )
        assert issued.status_code == 201
        assert "text/html" in issued.headers["content-type"]
        assert "umbra_" in issued.text
        secret = issued.text.split("umbra_", 1)[1].split("<", 1)[0]
        secret = "umbra_" + secret
        again = client.get("/keys")
        assert secret not in again.text
    saved = json.loads(keys_path_for(index).read_text(encoding="utf-8"))["keys"][0]
    assert saved["email"] == "ada@example.com"
    assert saved["organization"] == "Example Lab"
    assert secret not in keys_path_for(index).read_text(encoding="utf-8")


@pytestmark_public_app
def test_invalid_and_revoked_keys_are_401(tmp_path, monkeypatch):
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, cache_dir=tmp_path / "art")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.21"},
        )
        secret = issued.json()["key"]
        missing = client.get(
            "/search?limit=1",
            headers={"authorization": "Bearer umbra_not-a-real-key"},
        )
        assert missing.status_code == 401
        assert missing.json()["message"] == "API key is invalid."
        assert missing.headers["www-authenticate"].startswith("Bearer")
        mismatch = client.get(
            "/search?limit=1",
            headers={"authorization": f"Bearer {secret}", "x-api-key": "umbra_other"},
        )
        assert mismatch.status_code == 401
        by_header = client.get("/search?limit=1", headers={"x-api-key": secret})
        assert by_header.status_code == 200
        assert by_header.headers["x-ratelimit-limit"] == "600"
        app.state.api_keys.revoke("ada@example.com")
        revoked = client.get("/search?limit=1", headers={"authorization": f"Bearer {secret}"})
        assert revoked.status_code == 401
        assert "revoked" in revoked.json()["message"]
        anon = client.get("/search?limit=1", headers={"x-real-ip": "203.0.113.70"})
        assert anon.status_code == 200
        assert anon.headers["x-ratelimit-limit"] == "120"


@pytestmark_public_app
def test_keyed_burst_of_23_succeeds_where_anonymous_429s(tmp_path, monkeypatch):
    """23 /search calls fit in the keyed burst. The same address is already 429."""
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, cache_dir=tmp_path / "art")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.22"},
        )
        assert issued.status_code == 201, issued.text
        secret = issued.json()["key"]
        anon = {"x-real-ip": "203.0.113.9"}
        for _ in range(16):
            assert client.get("/search?limit=1", headers=anon).status_code == 200
        blocked = client.get("/search?limit=1", headers=anon)
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"]
        assert "per IP" in blocked.json()["message"]
        keyed = {"authorization": f"Bearer {secret}", "x-real-ip": "203.0.113.9"}
        for n in range(23):
            resp = client.get("/search?limit=1", headers=keyed)
            assert resp.status_code == 200, (n, resp.status_code, resp.text)
            assert resp.headers["x-ratelimit-limit"] == "600"


@pytestmark_public_app
def test_keyed_sustained_abuse_is_429_with_retry_after(tmp_path, monkeypatch):
    """A key still has a ceiling. Past it, the 429 names the key and Retry-After."""
    _fast_search(monkeypatch)
    monkeypatch.setenv("UMBRA_API_KEY_RATE_LIMIT", "4")
    monkeypatch.setenv("UMBRA_API_KEY_BURST", "4")
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, cache_dir=tmp_path / "art")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.26"},
        )
        secret = issued.json()["key"]
        headers = {"authorization": f"Bearer {secret}", "x-real-ip": "203.0.113.11"}
        for _ in range(4):
            assert client.get("/search?limit=1", headers=headers).status_code == 200
        over = client.get("/search?limit=1", headers=headers)
        assert over.status_code == 429
        assert int(over.headers["retry-after"]) >= 1
        assert "per key" in over.json()["message"]


@pytestmark_public_app
def test_keys_persist_across_an_app_restart(tmp_path, monkeypatch):
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, rate_limit=0, cache_dir=tmp_path / "a")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.23"},
        )
        secret = issued.json()["key"]
    restarted = serve.build_app(index, public=True, rate_limit=0, cache_dir=tmp_path / "b")
    with _client(restarted) as client:
        ok = client.get("/search?limit=1", headers={"authorization": f"Bearer {secret}"})
        assert ok.status_code == 200
        saved = json.loads(keys_path_for(index).read_text(encoding="utf-8"))["keys"][0]
        assert saved["last_used"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert secret not in json.dumps(saved)
        restarted.state.api_keys.revoke("ada@example.com")
        denied = client.get("/search?limit=1", headers={"authorization": f"Bearer {secret}"})
        assert denied.status_code == 401
        assert "revoked" in denied.json()["message"]


@pytestmark_public_app
def test_stats_hides_emails_and_still_skips_smoke(tmp_path, monkeypatch):
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, rate_limit=0, cache_dir=tmp_path / "art")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.24"},
        )
        secret = issued.json()["key"]
        smoke = client.get(
            "/search?limit=1",
            headers={"x-umbra-smoke": "1", "authorization": f"Bearer {secret}"},
        )
        assert smoke.status_code == 200
        real = client.get("/search?limit=1", headers={"x-real-ip": "203.0.113.80"})
        assert real.status_code == 200
        stats = client.get("/stats")
        assert stats.status_code == 200
        body = stats.text
        assert "ada@example.com" not in body
        assert secret not in body
        assert stats.json()["searches_served"] == 1


@pytestmark_public_app
def test_keyed_search_log_omits_the_address_and_the_secret(tmp_path, monkeypatch, caplog):
    _fast_search(monkeypatch)
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, rate_limit=0, cache_dir=tmp_path / "art")
    caplog.set_level(logging.INFO, logger="umbra_py.mcp.requests")
    with _client(app) as client:
        issued = client.post(
            "/keys",
            json={"email": "ada@example.com"},
            headers={"x-real-ip": "198.51.100.25"},
        )
        secret = issued.json()["key"]
        resp = client.get(
            "/search?limit=1",
            headers={"authorization": f"Bearer {secret}", "x-real-ip": "203.0.113.81"},
        )
        assert resp.status_code == 200
    lines = [
        json.loads(rec.message)
        for rec in caplog.records
        if rec.name == "umbra_py.mcp.requests" and rec.message.startswith("{")
    ]
    search_lines = [line for line in lines if line.get("msg") == "stac_search"]
    assert search_lines
    line = search_lines[-1]
    assert line["client_ip"] is None
    assert line["key_id"]
    assert len(line["key_id"]) == 12
    rendered = json.dumps(line)
    assert secret not in rendered
    assert "203.0.113.81" not in rendered
    assert "ada@example.com" not in rendered


@pytestmark_public_app
def test_mcp_post_with_a_bad_key_is_401(tmp_path):
    index = _index(tmp_path)
    app = serve.build_app(index, public=True, rate_limit=0, cache_dir=tmp_path / "art")
    with _client(app) as client:
        denied = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"authorization": "Bearer umbra_nope"},
        )
        assert denied.status_code == 401
        spec = client.get("/openapi.json").json()
        assert "/keys" in spec["paths"]
        ref = spec["paths"]["/keys"]["post"]["responses"]["201"]["content"]["application/json"]
        assert ref["schema"] == {"$ref": "#/components/schemas/ApiKeyIssued"}
        assert "ApiKeyIssued" in spec["components"]["schemas"]


@pytestmark_serve
def test_local_serve_has_no_key_route(tmp_path):
    index = _index(tmp_path)
    app = serve.build_app(index, cache_dir=tmp_path / "art")
    with _client(app) as client:
        assert client.get("/keys").status_code == 404
        spec = client.get("/openapi.json").json()
        assert "/keys" not in spec["paths"]
        assert "ApiKeyIssued" not in spec["components"]["schemas"]


def test_keys_cli_lists_email_and_revokes(tmp_path, monkeypatch, capsys):
    index = _index(tmp_path)
    moments = [1_700_000_000.0, 1_700_086_400.0]

    def clock() -> float:
        return moments.pop(0) if moments else 1_700_086_400.0

    store = ApiKeyStore(keys_path_for(index), clock=clock)
    signup = Signup("ada@example.com", "Ada", None, None)
    secret, earlier = store.create(signup)
    _other, later = store.create(signup)
    runner = CliRunner()
    listed = runner.invoke(cli, ["keys", "list", "--db", str(index)])
    assert listed.exit_code == 0
    assert "ada@example.com" in listed.output
    assert earlier.created_at in listed.output
    assert "active" in listed.output
    assert secret not in listed.output
    assert "sha256" not in listed.output
    help_list = runner.invoke(cli, ["keys", "list", "--help"])
    help_revoke = runner.invoke(cli, ["keys", "revoke", "--help"])
    assert "--db" in help_list.output
    assert "--db" in help_revoke.output
    assert "--created" in help_revoke.output
    ambiguous = runner.invoke(cli, ["keys", "revoke", "ada@example.com", "--db", str(index)])
    assert ambiguous.exit_code != 0
    assert "More than one active key" in ambiguous.output
    assert secret not in ambiguous.output
    monkeypatch.setattr(
        sys,
        "argv",
        ["umbra", "keys", "revoke", "ada@example.com", "--db", str(index)],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Pass --created" in err
    assert earlier.created_at in err
    assert later.created_at in err
    assert secret not in err
    assert "sha256" not in err
    revoked = runner.invoke(
        cli,
        [
            "keys",
            "revoke",
            "ada@example.com",
            "--created",
            earlier.created_at,
            "--db",
            str(index),
        ],
    )
    assert revoked.exit_code == 0, revoked.output
    assert secret not in revoked.output
    assert "sha256" not in revoked.output
    assert later.created_at not in revoked.output or earlier.created_at in revoked.output
    again = runner.invoke(cli, ["keys", "list", "--db", str(index)])
    assert "revoked" in again.output
    missing = runner.invoke(cli, ["keys", "list", "--db", str(tmp_path / "other" / "empty.db")])
    assert missing.exit_code == 0
    assert "No API keys." in missing.output


def test_hosted_limits_documents_the_key_and_the_cli():
    page = (REPO / "docs" / "hosted-limits.md").read_text(encoding="utf-8")
    assert "https://api.umbra-py.space/keys" in page
    assert "requests.post" in page
    assert 'headers={"Authorization": f"Bearer {key}"}' in page
    assert "X-API-Key" in page
    assert "umbra keys list --db" in page
    assert "umbra keys revoke" in page
    assert "--created" in page
    assert "UMBRA_API_KEY_RATE_LIMIT" in page
    assert "600" in page
    assert "burst of 64" in page or "burst of **64**" in page or "**burst of 64**" in page
    assert "api-keys.json" in page
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "POST /keys" in changelog
    assert "umbria" not in page.lower()
    assert "umbria" not in changelog.lower()
