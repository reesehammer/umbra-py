"""Tests for forward geocoding (place name -> bbox) and the CLI --place wiring."""

import pytest
import responses

from umbra_py.exceptions import GeocodeError
from umbra_py.geocode import (
    _GEOCODE_MAX_WAIT_S,
    PublicGeocodeGate,
    _parse_boundingbox,
    geocode_for_host,
    geocode_place,
)
from umbra_py.models import UmbraItem

_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim returns the box as [south, north, west, east]; we expect it
# reordered to (min_lon, min_lat, max_lon, max_lat).
_CALIFORNIA_BBOX = ["32.5", "42.0", "-124.4", "-114.1"]
_CALIFORNIA_EXPECTED = (-124.4, 32.5, -114.1, 42.0)


def test_parse_boundingbox_reorders_to_lonlat():
    assert _parse_boundingbox(_CALIFORNIA_BBOX) == _CALIFORNIA_EXPECTED


def test_parse_boundingbox_rejects_bad_shape():
    assert _parse_boundingbox(None) is None
    assert _parse_boundingbox(["1", "2", "3"]) is None  # too few
    assert _parse_boundingbox(["a", "b", "c", "d"]) is None  # not numbers


@responses.activate
def test_geocode_place_returns_bbox_and_label():
    responses.add(
        responses.GET,
        _SEARCH_URL,
        json=[{"boundingbox": _CALIFORNIA_BBOX, "display_name": "California, United States"}],
        status=200,
    )
    bbox, label = geocode_place("California")
    assert bbox == _CALIFORNIA_EXPECTED
    assert label == "California, United States"


def test_geocode_place_empty_query_raises():
    # Bails before any network call.
    with pytest.raises(GeocodeError):
        geocode_place("   ")


@responses.activate
def test_geocode_place_no_match_raises():
    responses.add(responses.GET, _SEARCH_URL, json=[], status=200)
    with pytest.raises(GeocodeError, match="No place matched"):
        geocode_place("asdfqwerty nowhere")


@responses.activate
def test_geocode_place_http_error_raises():
    responses.add(responses.GET, _SEARCH_URL, status=503)
    with pytest.raises(GeocodeError):
        geocode_place("California")


@responses.activate
def test_geocode_place_missing_bbox_raises():
    responses.add(responses.GET, _SEARCH_URL, json=[{"display_name": "Somewhere"}], status=200)
    with pytest.raises(GeocodeError):
        geocode_place("Somewhere")


# -- CLI --place wiring ----------------------------------------------------


def test_cli_search_place_resolves_to_bbox(monkeypatch):
    """`umbra search --place California` geocodes the name and searches that
    bbox, echoing the resolved place."""
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([])

    monkeypatch.setattr(
        "umbra_py.cli._shared.geocode_place", lambda _q: (_CALIFORNIA_EXPECTED, "California, USA")
    )
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)

    result = CliRunner().invoke(cli_mod.cli, ["search", "--place", "California"])
    assert result.exit_code == 0, result.output
    assert "Resolved 'California' to California, USA." in result.output
    assert captured["bbox"] == _CALIFORNIA_EXPECTED


def test_cli_search_place_and_bbox_conflict(monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    def boom(_q):  # pragma: no cover - must not be reached
        raise AssertionError("geocode should not run when both options are given")

    monkeypatch.setattr("umbra_py.cli._shared.geocode_place", boom)
    result = CliRunner().invoke(cli_mod.cli, ["search", "--place", "X", "--bbox", "0,0,1,1"])
    assert result.exit_code != 0
    assert "not both" in result.output.lower()


def test_cli_search_place_not_found_reports_cleanly(monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    def boom(_q):
        raise GeocodeError("No place matched 'zzz'.")

    monkeypatch.setattr("umbra_py.cli._shared.geocode_place", boom)
    result = CliRunner().invoke(cli_mod.cli, ["search", "--place", "zzz"])
    assert result.exit_code != 0
    assert "No place matched" in result.output


def test_cli_map_place_resolves_to_bbox(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([])

    monkeypatch.setattr(
        "umbra_py.cli._shared.geocode_place", lambda _q: (_CALIFORNIA_EXPECTED, "California")
    )
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)

    # No items -> ClickException, but the geocode + bbox wiring still ran.
    result = CliRunner().invoke(
        cli_mod.cli, ["map", "--place", "California", "--out", str(tmp_path / "m.geojson")]
    )
    assert "Resolved 'California'" in result.output
    assert captured["bbox"] == _CALIFORNIA_EXPECTED


def test_cli_timescan_place_resolves_to_bbox(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([])

    monkeypatch.setattr(
        "umbra_py.cli._shared.geocode_place", lambda _q: (_CALIFORNIA_EXPECTED, "California")
    )
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)

    # No items -> ClickException ("need at least 3"), but the geocode + bbox
    # wiring still ran first.
    result = CliRunner().invoke(
        cli_mod.cli, ["timescan", "--place", "California", "--out", str(tmp_path / "t.png")]
    )
    assert "Resolved 'California'" in result.output
    assert captured["bbox"] == _CALIFORNIA_EXPECTED


def test_cli_timescan_place_and_bbox_conflict(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    def boom(_q):  # pragma: no cover - must not be reached
        raise AssertionError("geocode should not run when both options are given")

    monkeypatch.setattr("umbra_py.cli._shared.geocode_place", boom)
    result = CliRunner().invoke(
        cli_mod.cli,
        ["timescan", "--place", "X", "--bbox", "0,0,1,1", "--out", str(tmp_path / "t.png")],
    )
    assert result.exit_code != 0
    assert "not both" in result.output.lower()


def test_cli_gallery_place_resolves_and_labels_page(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.viz import contact_sheet as viz_mod

    captured: dict = {}

    def fake_search(self, **kwargs):
        captured.update(kwargs)
        return iter([UmbraItem(id="x", bbox=(-120.0, 35.0, -119.0, 36.0))])

    monkeypatch.setattr(
        "umbra_py.cli._shared.geocode_place", lambda _q: (_CALIFORNIA_EXPECTED, "California")
    )
    monkeypatch.setattr("umbra_py.cli._shared.UmbraCatalog.search", fake_search)
    monkeypatch.setattr(viz_mod, "_require", lambda *_a, **_k: None)
    monkeypatch.setattr(viz_mod, "_thumbnail_data_uri", lambda *_a, **_k: "data:image/png;base64,Z")

    out = tmp_path / "g.html"
    result = CliRunner().invoke(
        cli_mod.cli, ["gallery", "--place", "California", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert captured["bbox"] == _CALIFORNIA_EXPECTED
    assert "California" in out.read_text()  # place surfaces in the page header


def test_public_geocode_caches_a_hit_and_spaces_callers(monkeypatch):
    clock = {"now": 50.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    gate = PublicGeocodeGate(min_interval=1.0, time_fn=lambda: clock["now"], sleep_fn=sleep)
    calls: list[str] = []

    def fake(query: str, **_kwargs):
        calls.append(query)
        return ((0.0, 0.0, 1.0, 1.0), query.strip())

    monkeypatch.setattr("umbra_py.geocode.geocode_place", fake)
    assert gate.resolve("Paris")[1] == "Paris"
    assert gate.resolve(" paris ")[1] == "Paris"
    assert calls == ["Paris"]
    assert slept == []
    gate.resolve("Lyon")
    assert calls == ["Paris", "Lyon"]
    assert slept == [1.0]


def test_local_geocode_for_host_caches_without_throttling(monkeypatch):
    """Local serve reuses the LRU and the negative cache, and does not wait."""
    from umbra_py.geocode import _public_geocode

    _public_geocode.clear()
    calls: list[str] = []
    slept: list[float] = []

    def fake(query: str, **_kwargs):
        calls.append(query)
        if query == "zzz":
            raise GeocodeError("No place matched 'zzz'.")
        if query == "down":
            raise GeocodeError("Could not reach the geocoding service: timeout")
        return ((0.0, 1.0, 2.0, 3.0), query)

    monkeypatch.setattr("umbra_py.geocode.geocode_place", fake)
    monkeypatch.setattr(_public_geocode, "_sleep", lambda seconds: slept.append(seconds))
    assert geocode_for_host("A", public=False)[1] == "A"
    assert geocode_for_host("A", public=False)[1] == "A"
    geocode_for_host("B", public=False)
    assert calls == ["A", "B"]
    assert slept == []
    with pytest.raises(GeocodeError, match="No place matched"):
        geocode_for_host("zzz", public=False)
    with pytest.raises(GeocodeError, match="No place matched"):
        geocode_for_host("zzz", public=False)
    assert calls == ["A", "B", "zzz"]
    with pytest.raises(GeocodeError, match="Could not reach"):
        geocode_for_host("down", public=False)
    with pytest.raises(GeocodeError, match="Could not reach"):
        geocode_for_host("down", public=False)
    assert calls[-2:] == ["down", "down"]


def test_public_geocode_caches_a_miss_and_not_a_network_failure(monkeypatch):
    clock = {"now": 0.0}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    gate = PublicGeocodeGate(
        max_size=8, min_interval=1.0, time_fn=lambda: clock["now"], sleep_fn=sleep
    )
    calls: list[str] = []

    def fake(query: str, **_kwargs):
        calls.append(query)
        if query == "zzz":
            raise GeocodeError("No place matched 'zzz'.")
        if query == "down":
            raise GeocodeError("Could not reach the geocoding service: timeout")
        return ((0.0, 0.0, 1.0, 1.0), query)

    monkeypatch.setattr("umbra_py.geocode.geocode_place", fake)
    with pytest.raises(GeocodeError, match="No place matched"):
        gate.resolve("zzz")
    with pytest.raises(GeocodeError, match="No place matched"):
        gate.resolve("zzz")
    assert calls == ["zzz"]
    clock["now"] += 601
    with pytest.raises(GeocodeError, match="No place matched"):
        gate.resolve("zzz")
    assert calls == ["zzz", "zzz"]
    with pytest.raises(GeocodeError, match="Could not reach"):
        gate.resolve("down")
    with pytest.raises(GeocodeError, match="Could not reach"):
        gate.resolve("down")
    assert calls[-2:] == ["down", "down"]


def test_public_geocode_fails_fast_when_busy(monkeypatch):
    clock = {"now": 0.0}
    gate = PublicGeocodeGate(
        min_interval=1.0,
        time_fn=lambda: clock["now"],
        sleep_fn=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    calls: list[str] = []

    def fake(query: str, **_kwargs):
        calls.append(query)
        return ((0.0, 0.0, 1.0, 1.0), query)

    monkeypatch.setattr("umbra_py.geocode.geocode_place", fake)
    gate._waiters = 5
    with pytest.raises(GeocodeError, match="retry"):
        gate.resolve("Paris")
    assert calls == []
    gate._waiters = 0
    gate._next_at = clock["now"] + 5.1
    with pytest.raises(GeocodeError, match="retry"):
        gate.resolve("Paris")
    assert calls == []

    class _TimedOut:
        def acquire(self, timeout: float = -1) -> bool:
            assert 0 < timeout <= _GEOCODE_MAX_WAIT_S
            return False

        def release(self) -> None:
            raise AssertionError("the lock was not acquired")

    gate._network = _TimedOut()  # type: ignore[assignment]
    gate._next_at = 0.0
    with pytest.raises(GeocodeError, match="retry"):
        gate.resolve("Paris")
    assert calls == []
    assert gate._waiters == 0
