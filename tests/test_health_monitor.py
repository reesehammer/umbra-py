"""Offline tests for the hosted /healthz probe and its history branch."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from umbra_py import health_monitor as hm
from umbra_py import serve
from umbra_py.constants import STAC_BUILT_AT_FIELD, STAC_SNAPSHOT_FIELD

REPO = Path(__file__).resolve().parents[1]


def _record(**overrides):
    base = {
        "probed_at": "2026-10-15T12:00:00+00:00",
        "url": hm.HEALTHZ_URL,
        "status_code": 200,
        "latency_ms": 40,
        "error": None,
        "ok": True,
        "reason": None,
        "degraded": False,
        "ready": True,
        "status": "ok",
        "built_at": "2026-10-14",
        "snapshot": "etag",
        "stale": False,
        "items": 3,
        "ids": 3,
    }
    base.update(overrides)
    return base


def test_stale_boundary_is_more_than_nine_days():
    today = date(2026, 10, 15)
    assert hm.index_is_stale("2026-10-06", today=today) is False  # exactly 9 days
    assert hm.index_is_stale("2026-10-05", today=today) is True
    assert hm.index_is_stale("2026-10-15T06:17:00Z", today=today) is False
    assert hm.index_is_stale(None, today=today) is True
    assert hm.index_is_stale("not-a-date", today=today) is True
    assert hm.INDEX_STALE_AFTER_DAYS == 9


def test_evaluate_health_requires_200_and_degraded_false():
    today = date(2026, 10, 15)
    fresh = {
        "status": "ok",
        "ready": True,
        "degraded": False,
        "built_at": "2026-10-14",
        "snapshot": "v",
        "items": 1,
        "ids": 1,
    }
    ok = hm.evaluate_health(200, fresh, today=today)
    assert ok["ok"] is True and ok["stale"] is False
    assert ok["reason"] is None

    degraded = hm.evaluate_health(200, {**fresh, "degraded": True}, today=today)
    assert degraded["ok"] is False
    assert degraded["reason"] == "degraded is not false"
    assert degraded["stale"] is False  # freshness is not the outage

    down = hm.evaluate_health(503, {"status": "error", "ready": False}, today=today)
    assert down["ok"] is False and down["stale"] is None

    old = hm.evaluate_health(200, {**fresh, "built_at": "2026-10-01"}, today=today)
    assert old["ok"] is True and old["stale"] is True

    # A server that has not shipped `stale` yet is still judged from built_at.
    assert "stale" not in fresh
    assert old["stale"] is True


def test_probe_once_records_latency_and_transport_errors():
    def opener(url, timeout):
        assert url == "https://example.test/healthz"
        assert timeout == hm.PROBE_TIMEOUT_S
        return 200, {"degraded": False, "ready": True, "built_at": "2026-10-14", "status": "ok"}

    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    record = hm.probe_once(
        "https://example.test/healthz",
        opener=opener,
        now=now,
        today=date(2026, 10, 15),
    )
    assert record["ok"] is True
    assert record["status_code"] == 200
    assert record["probed_at"] == "2026-10-15T12:00:00+00:00"
    assert isinstance(record["latency_ms"], int) and record["latency_ms"] >= 0
    assert record["error"] is None

    def boom(url, timeout):
        raise TimeoutError("timed out")

    failed = hm.probe_once("https://example.test/healthz", opener=boom, today=date(2026, 10, 15))
    assert failed["ok"] is False
    assert failed["status_code"] is None
    assert failed["stale"] is None
    assert "TimeoutError" in failed["error"]


def test_sustained_failure_and_issue_actions():
    down = [_record(ok=False), _record(ok=False)]
    assert hm.sustained_failure(down) is False
    down.append(_record(ok=False))
    assert hm.sustained_failure(down) is True
    assert hm.sustained_failure([*down, _record(ok=True)]) is False

    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    assert (
        hm.health_issue_action(
            ok=False, sustained=False, issue_open=False, updated_at=None, now=now
        )
        is None
    )
    assert (
        hm.health_issue_action(ok=False, sustained=True, issue_open=False, updated_at=None, now=now)
        == "open"
    )
    recent = now - timedelta(minutes=15)
    assert (
        hm.health_issue_action(
            ok=False, sustained=True, issue_open=True, updated_at=recent, now=now
        )
        is None
    )
    stale_comment = now - hm.ISSUE_UPDATE_INTERVAL - timedelta(seconds=1)
    assert (
        hm.health_issue_action(
            ok=False, sustained=True, issue_open=True, updated_at=stale_comment, now=now
        )
        == "comment"
    )
    assert (
        hm.health_issue_action(ok=True, sustained=True, issue_open=True, updated_at=recent, now=now)
        == "recover"
    )

    assert hm.stale_issue_action(stale=True, issue_open=False, updated_at=None, now=now) == "open"
    assert hm.stale_issue_action(stale=None, issue_open=True, updated_at=recent, now=now) is None
    assert (
        hm.stale_issue_action(stale=False, issue_open=True, updated_at=recent, now=now) == "recover"
    )


def test_uptime_ratio_is_withheld_until_seven_days():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    short = [
        _record(probed_at=start.isoformat(), ok=True),
        _record(probed_at=(start + timedelta(days=6)).isoformat(), ok=False),
    ]
    assert hm.history_span_days(short) == pytest.approx(6.0)
    assert hm.uptime_ratio(short) is None
    long = [
        *short,
        _record(probed_at=(start + timedelta(days=7)).isoformat(), ok=True),
    ]
    # Two up, one down. The stale bit on a row does not remove it from uptime.
    assert hm.uptime_ratio(long) == pytest.approx(2 / 3)
    assert hm.HISTORY_MIN_DAYS == 7


class _Gh:
    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.next_number = 1
        self.calls: list[list[str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        command = args[2]
        if command == "list":
            query = args[args.index("--search") + 1]
            title = query.split(" in:title", 1)[0]
            row = self.issues.get(title)
            payload = (
                []
                if row is None or row.get("closed")
                else [
                    {
                        "number": row["number"],
                        "title": title,
                        "updatedAt": row["updated"].isoformat().replace("+00:00", "Z"),
                    }
                ]
            )
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if command == "create":
            title = args[args.index("--title") + 1]
            self.issues[title] = {
                "number": self.next_number,
                "updated": datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
                "closed": False,
                "body": args[args.index("--body") + 1],
            }
            self.next_number += 1
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if command == "comment":
            number = int(args[3])
            for row in self.issues.values():
                if row["number"] == number:
                    row["body"] = args[args.index("--body") + 1]
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if command == "close":
            number = int(args[3])
            for row in self.issues.values():
                if row["number"] == number:
                    row["closed"] = True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)


def test_alert_opens_on_sustained_failure_and_closes_on_recovery():
    gh = _Gh()
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    history = [_record(ok=False), _record(ok=False), _record(ok=False, reason="status 503")]
    hm.alert_from_history(history, history[-1], env={}, run=gh, now=now)
    assert hm.HEALTH_ISSUE_TITLE in gh.issues
    assert "status 503" in gh.issues[hm.HEALTH_ISSUE_TITLE]["body"]

    # Still failing, but the issue was just updated: no second comment.
    before = len(gh.calls)
    hm.alert_from_history(history + [history[-1]], history[-1], env={}, run=gh, now=now)
    assert len(gh.calls) == before + 1  # the list, and then nothing else
    assert gh.issues[hm.HEALTH_ISSUE_TITLE]["closed"] is False

    recovered = _record(ok=True)
    hm.alert_from_history([*history, recovered], recovered, env={}, run=gh, now=now)
    assert gh.issues[hm.HEALTH_ISSUE_TITLE]["closed"] is True


def test_stale_issue_is_separate_from_uptime():
    gh = _Gh()
    now = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    stale = _record(ok=True, stale=True, built_at="2026-10-01")
    hm.alert_from_history([stale], stale, env={}, run=gh, now=now)
    assert hm.STALE_ISSUE_TITLE in gh.issues
    assert hm.HEALTH_ISSUE_TITLE not in gh.issues
    assert "2026-10-01" in gh.issues[hm.STALE_ISSUE_TITLE]["body"]

    fresh = _record(ok=True, stale=False, built_at="2026-10-14")
    hm.alert_from_history([stale, fresh], fresh, env={}, run=gh, now=now)
    assert gh.issues[hm.STALE_ISSUE_TITLE]["closed"] is True


def test_main_exit_code_follows_ok_and_stale_without_publishing_a_ratio(capsys):
    down = _record(ok=False, stale=False, reason="status None")
    code = hm.main(
        env={},
        probe=lambda url: down,
        publish=lambda record: [record],
        alert=lambda h, r: None,
    )
    assert code == 1
    assert "Uptime percentage withheld" in capsys.readouterr().out

    fresh = _record(ok=True, stale=False)
    assert (
        hm.main(
            env={},
            probe=lambda url: fresh,
            publish=lambda record: [record],
            alert=lambda h, r: None,
        )
        == 0
    )

    old = _record(ok=True, stale=True, built_at="2026-10-01")
    assert (
        hm.main(
            env={},
            probe=lambda url: old,
            publish=lambda record: [record],
            alert=lambda h, r: None,
        )
        == 1
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("main\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "HEAD:main")
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-b", "main", str(origin), str(work)],
        check=True,
        capture_output=True,
    )
    return origin, work


def test_history_is_appended_on_the_monitoring_branch_only(tmp_path):
    origin, work = _repo(tmp_path)
    main_before = subprocess.check_output(["git", "-C", str(work), "rev-parse", "main"], text=True)
    first = _record(probed_at="2026-10-15T12:00:00+00:00")
    history = hm.publish_history(work, first, push=True)
    assert len(history) == 1
    second = _record(probed_at="2026-10-15T12:15:00+00:00", ok=False)
    history = hm.publish_history(work, second, push=True)
    assert [row["probed_at"] for row in history] == [
        "2026-10-15T12:00:00+00:00",
        "2026-10-15T12:15:00+00:00",
    ]

    checked = tmp_path / "checked"
    subprocess.run(
        ["git", "clone", "--branch", hm.HISTORY_BRANCH, str(origin), str(checked)],
        check=True,
        capture_output=True,
    )
    names = sorted(path.name for path in checked.iterdir() if path.name != ".git")
    assert names == ["README.md", hm.HISTORY_FILE]
    lines = (checked / hm.HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["ok"] is False

    main = tmp_path / "main"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(main)],
        check=True,
        capture_output=True,
    )
    assert (main / "README.md").read_text(encoding="utf-8") == "main\n"
    assert not (main / hm.HISTORY_FILE).exists()
    main_after = subprocess.check_output(["git", "-C", str(work), "rev-parse", "main"], text=True)
    assert main_after == main_before


def test_limits_page_matches_the_server_constants():
    page = (REPO / "docs" / "hosted-limits.md").read_text(encoding="utf-8")
    assert serve.PUBLIC_RATE_LIMIT == 120
    assert int(serve.PUBLIC_RATE_WINDOW_S) == 60
    assert serve.MAX_LIMIT == 10_000
    assert hm.INDEX_STALE_AFTER_DAYS == 9
    assert hm.SUSTAINED_FAILURES == 3
    assert hm.HISTORY_MIN_DAYS == 7
    assert f"{serve.PUBLIC_RATE_LIMIT} requests" in page
    assert f"{int(serve.PUBLIC_RATE_WINDOW_S)}-second" in page
    assert f"{serve.MAX_LIMIT:,}" in page
    assert f"{hm.INDEX_STALE_AFTER_DAYS} days" in page
    assert "Three consecutive" in page
    assert "7 days" in page
    assert "99%" in page
    assert hm.HISTORY_BRANCH in page
    assert serve.CORS_ALLOW_ORIGINS == ("*",)
    assert "`*`" in page
    for method in serve.CORS_ALLOW_METHODS:
        assert f"`{method}`" in page
    for header in serve.CORS_EXPOSE_HEADERS:
        assert header in page
    assert STAC_BUILT_AT_FIELD in page
    assert STAC_SNAPSHOT_FIELD in page
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "hosted-limits" in readme
    nav = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    assert "hosted-limits.md" in nav


def test_healthz_workflow_stays_off_main_and_pins_the_runner():
    path = REPO / ".github" / "workflows" / "healthz.yml"
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    trigger = document[True]
    assert set(trigger) == {"schedule", "workflow_dispatch"}
    assert trigger["schedule"] == [{"cron": "*/15 * * * *"}]
    job = document["jobs"]["probe"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert document["permissions"]["contents"] == "write"
    assert document["permissions"]["issues"] == "write"
    script = "\n".join(step.get("run") or "" for step in job["steps"])
    assert "src/umbra_py/health_monitor.py" in script
    assert "src/umbra_py/mcp_smoke.py" in script
    assert "pip install" not in script
    mcp_step = next(
        step for step in job["steps"] if step.get("name") == "Probe MCP stable tool names"
    )
    assert mcp_step["if"] == "${{ !cancelled() }}"
    assert "ubuntu-latest" not in text
    source = (REPO / "src" / "umbra_py" / "health_monitor.py").read_text(encoding="utf-8")
    assert "import umbra_py" not in source
    assert "\nfrom ." not in source
    assert f'HISTORY_BRANCH = "{hm.HISTORY_BRANCH}"' in source


def test_publish_workflow_uses_the_coverage_gate_constant():
    text = (REPO / ".github" / "workflows" / "publish-index.yml").read_text(encoding="utf-8")
    assert "COVERAGE_GATE_PCT" in text
