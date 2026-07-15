from umbra_py._http import default_session


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
