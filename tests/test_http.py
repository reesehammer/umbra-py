import pytest

from umbra_py import _http
from umbra_py._http import default_session, post_model_json


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
