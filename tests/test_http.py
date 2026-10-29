import select
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from umbra_py import _http
from umbra_py._http import default_session, post_model_json, safe_get


def test_default_session_mounts_retries():
    session = default_session()
    for scheme in ("https://", "http://"):
        adapter = session.get_adapter(scheme + "s3.amazonaws.com/")
        retries = adapter.max_retries
        assert retries.total == 3
        assert retries.backoff_factor == 0.5
        # The transient/throttling codes S3 uses must be retried.
        for code in (429, 500, 502, 503, 504):
            assert code in retries.status_forcelist


# --- post_model_json: retry + surface the provider's error --------------------


class _Err(Exception):
    """Stand-in for a module's own error type (DescribeError / AskError)."""


class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually wait between retries in tests."""
    monkeypatch.setattr(_http.time, "sleep", lambda *a: None)


def _stub_posts(monkeypatch, responses):
    """Feed post_model_json a sequence of _Resp objects (or exceptions to raise),
    one per call, and record how many times it posted."""
    seq = list(responses)
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_http.requests, "post", fake_post)
    return calls


def test_post_model_json_returns_a_good_body(monkeypatch):
    calls = _stub_posts(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": "ok"}}]})])
    out = post_model_json("u", {}, {}, error_cls=_Err)
    assert out["choices"][0]["message"]["content"] == "ok"
    assert calls["n"] == 1  # a good response is not retried


def test_post_model_json_retries_a_200_error_body_then_succeeds(monkeypatch):
    """OpenRouter's HTTP-200-with-error body (an upstream provider blip) is the
    exact case that skipped one featured narration; it must be retried."""
    calls = _stub_posts(
        monkeypatch,
        [
            _Resp(200, {"error": {"message": "upstream provider error", "code": 502}}),
            _Resp(200, {"choices": [{"message": {"content": "recovered"}}]}),
        ],
    )
    out = post_model_json("u", {}, {}, error_cls=_Err)
    assert out["choices"][0]["message"]["content"] == "recovered"
    assert calls["n"] == 2  # first attempt errored, second succeeded


def test_post_model_json_surfaces_the_providers_message_on_persistent_error(monkeypatch):
    calls = _stub_posts(
        monkeypatch,
        [_Resp(200, {"error": {"message": "content flagged by moderation"}})] * 3,
    )
    with pytest.raises(_Err, match="content flagged by moderation"):
        post_model_json("u", {}, {}, error_cls=_Err, attempts=3)
    assert calls["n"] == 3  # exhausted the attempts


def test_post_model_json_does_not_retry_a_non_transient_status(monkeypatch):
    """A 402 (out of credit) or 400/401 is a verdict, not a blip: raise at once."""
    calls = _stub_posts(monkeypatch, [_Resp(402, {"error": {"message": "insufficient credits"}})])
    with pytest.raises(_Err, match="HTTP 402"):
        post_model_json("u", {}, {}, error_cls=_Err)
    assert calls["n"] == 1  # no retry


def test_post_model_json_retries_a_5xx_and_a_dropped_connection(monkeypatch):
    import requests

    calls = _stub_posts(
        monkeypatch,
        [
            requests.ConnectionError("connection reset"),
            _Resp(503, "service unavailable"),
            _Resp(200, {"choices": [{"message": {"content": "third time"}}]}),
        ],
    )
    out = post_model_json("u", {}, {}, error_cls=_Err, attempts=3)
    assert out["choices"][0]["message"]["content"] == "third time"
    assert calls["n"] == 3


# --- safe_get: allowlist, no redirects, size cap, total deadline ---------------


def test_safe_get_rejects_a_disallowed_url_before_connecting():
    with pytest.raises(ValueError, match="not allowed"):
        safe_get("https://evil.example/secret", lambda _url: False)


@contextmanager
def _listener():
    """Local stand-in. Records every path it is asked for."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/redir"):
                host, port = self.server.server_address[:2]
                self.send_response(302)
                self.send_header("Location", f"http://{host}:{port}/secret")
                self.end_headers()
                return
            if self.path.startswith("/stall"):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                # select, not time.sleep: the HTTP tests stub the latter.
                select.select([], [], [], 5)
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    return
                return
            if self.path.startswith("/big"):
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(b"x" * 64_000)
                except (BrokenPipeError, ConnectionError, OSError):
                    return
                return
            if self.path.startswith("/err"):
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"no")
                return
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", hits
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_safe_get_does_not_connect_to_a_disallowed_host():
    with _listener() as (base, hits):
        with pytest.raises(ValueError, match="not allowed"):
            safe_get(base + "/item", lambda _url: False)
        assert hits == []


def test_safe_get_does_not_follow_a_redirect():
    with _listener() as (base, hits):
        with pytest.raises(ValueError, match="redirects are not followed"):
            safe_get(base + "/redir", lambda _url: True)
        assert hits == ["/redir"]


def test_safe_get_stops_past_max_bytes():
    with _listener() as (base, hits):
        with pytest.raises(ValueError, match="exceeds 32 bytes"):
            safe_get(base + "/big", lambda _url: True, max_bytes=32)
        assert hits == ["/big"]


def test_safe_get_deadline_stops_a_stalled_body():
    """A body that never arrives dies on the deadline, not the server's stall."""
    with _listener() as (base, _hits):
        started = time.monotonic()
        with pytest.raises(ValueError, match="deadline"):
            safe_get(base + "/stall", lambda _url: True, total_deadline=0.6)
        assert time.monotonic() - started < 3.0


def test_safe_get_does_not_retry_a_500():
    with _listener() as (base, hits):
        with pytest.raises(requests.HTTPError):
            safe_get(base + "/err", lambda _url: True)
        assert hits == ["/err"]


def test_safe_get_reads_an_allowlisted_body():
    with _listener() as (base, hits):
        raw = safe_get(base + "/item", lambda _url: True)
        assert raw == b'{"ok": true}'
        assert hits == ["/item"]
