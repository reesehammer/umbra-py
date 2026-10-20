"""External probe of the hosted community API.

``GET /healthz`` on the process itself cannot see a dead proxy, a bad deploy,
or a build that stopped landing. The scheduled workflow runs this module from
GitHub Actions: one HTTPS request, a line appended to ``healthz.jsonl`` on the
``monitoring`` branch (never ``main``), and a GitHub issue when the failure
lasts or the index build is too old.

Uptime is the ``ok`` field (HTTP 200 and ``degraded`` false). A stale index is
recorded separately and does not count as downtime. The ratio is not printed:
the history has to cover seven days before a later report publishes a number.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Public community API. The workflow probes this URL and nothing else.
HEALTHZ_URL = "https://api.umbra-py.space/healthz"

#: An index whose ``built_at`` is older than this many days is stale. The
#: weekly publish is Mondays 06:17 UTC, so nine days means a rebuild was missed.
INDEX_STALE_AFTER_DAYS = 9

#: Consecutive failed probes before an issue is opened. At a 15-minute schedule
#: that is about 45 minutes, long enough to skip a single blip.
SUSTAINED_FAILURES = 3

#: How long an open issue stays quiet between comments while the problem holds.
ISSUE_UPDATE_INTERVAL = timedelta(hours=12)

#: Do not publish an uptime percentage until the history spans this many days.
HISTORY_MIN_DAYS = 7

#: Branch that holds ``healthz.jsonl``. Pushes here must not touch ``main``.
HISTORY_BRANCH = "monitoring"
HISTORY_FILE = "healthz.jsonl"

#: A cold ``/healthz`` on the hosted volume has taken about 25s, and a 20s
#: client timeout reported that as "no status" (an outage) for a request that
#: was still reading. 60s lets a slow-but-alive read finish. The server's own
#: catalog deadline is shorter, so a stuck read is a 503 inside this budget.
PROBE_TIMEOUT_S = 60.0

#: GitHub login assigned to, and mentioned on, a hosted-API failure issue.
HEALTH_ISSUE_ASSIGNEE = "reesehammer"

HEALTH_ISSUE_TITLE = "Hosted API healthz failing"
STALE_ISSUE_TITLE = "Hosted catalog index is stale"

MONITORING_README = """\
# Hosted API probe history

Append-only `healthz.jsonl`, one JSON object per probe of
`https://api.umbra-py.space/healthz`. Written by
`.github/workflows/healthz.yml` on this branch only. `main` is not updated.

`ok` is the uptime bit: HTTP 200 and `degraded` false. `stale` is a separate
freshness bit (`built_at` more than 9 days old) and is not downtime.
`latency_ms` is how long the request took. There is no latency cap.

An uptime percentage is withheld until this file covers 7 days.
"""

_USER_AGENT = "umbra-py-healthz-monitor"
_Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def utc_today() -> date:
    """The UTC date the probe and ``/healthz`` both judge ``built_at`` against."""
    return datetime.now(timezone.utc).date()


def parse_built_at(value: object) -> date | None:
    """``YYYY-MM-DD`` (or a datetime with that date prefix), else ``None``."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def index_is_stale(
    built_at: object,
    *,
    today: date | None = None,
    after_days: int = INDEX_STALE_AFTER_DAYS,
) -> bool:
    """Whether an index build date is more than ``after_days`` old.

    A missing or unparseable ``built_at`` is stale: a monitor cannot treat an
    unknown build as current. A build exactly ``after_days`` old is still
    fresh; the next day is not. Dates are UTC calendar days, matching
    ``built_at`` (the workflow stamps ``date.today()`` in UTC).
    """
    today = utc_today() if today is None else today
    parsed = parse_built_at(built_at)
    if parsed is None:
        return True
    return (today - parsed).days > after_days


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_health(
    status_code: int | None,
    body: Mapping[str, Any] | None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Decide uptime and freshness from one ``/healthz`` response.

    ``ok`` is HTTP 200 and ``degraded`` exactly false. Latency is not part of
    the decision: the server has no latency cap, so the probe only records it.
    ``stale`` is ``None`` when the body cannot speak to the build date (the
    request failed, or the server is still starting). A ready index with no
    ``built_at`` is stale.
    """
    degraded = body.get("degraded") if isinstance(body, Mapping) else None
    ready = body.get("ready") if isinstance(body, Mapping) else None
    built_raw = body.get("built_at") if isinstance(body, Mapping) else None
    built_at = built_raw if isinstance(built_raw, str) else None
    snap_raw = body.get("snapshot") if isinstance(body, Mapping) else None
    snapshot = snap_raw if isinstance(snap_raw, str) else None
    reasons: list[str] = []
    if status_code != 200:
        reasons.append(f"status {status_code}")
    if degraded is not False:
        reasons.append("degraded is not false")
    stale: bool | None = None
    if isinstance(body, Mapping) and (built_at is not None or "stale" in body or ready is True):
        server_stale = body.get("stale") is True
        stale = server_stale or index_is_stale(built_at, today=today)
    status = body.get("status") if isinstance(body, Mapping) else None
    items = body.get("items") if isinstance(body, Mapping) else None
    ids = body.get("ids") if isinstance(body, Mapping) else None
    return {
        "ok": not reasons,
        "reason": "; ".join(reasons) if reasons else None,
        "degraded": degraded if isinstance(degraded, bool) else None,
        "ready": ready if isinstance(ready, bool) else None,
        "status": status if isinstance(status, str) else None,
        "built_at": built_at,
        "snapshot": snapshot,
        "stale": stale,
        "items": items if isinstance(items, int) else None,
        "ids": ids if isinstance(ids, int) else None,
    }


def _http_get_json(url: str, timeout: float) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_000)
            code = int(response.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_000)
        code = int(exc.code)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    return code, body


def probe_once(
    url: str = HEALTHZ_URL,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    opener: Callable[[str, float], tuple[int, Any]] | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Fetch ``url`` and return one history record, including latency.

    Transport errors (timeout, DNS) are a failed probe with ``status_code``
    null. ``opener`` replaces the HTTP call in tests.
    """
    started = time.perf_counter()
    status: int | None = None
    body: Any = None
    error: str | None = None
    fetch = opener or _http_get_json
    try:
        status, body = fetch(url, timeout)
    except Exception as exc:  # URLError, timeout, or a test opener
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = int(round((time.perf_counter() - started) * 1000))
    evaluated = evaluate_health(status, body if isinstance(body, dict) else None, today=today)
    probed = now or datetime.now(timezone.utc)
    if probed.tzinfo is None:
        probed = probed.replace(tzinfo=timezone.utc)
    return {
        "probed_at": probed.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        "status_code": status,
        "latency_ms": latency_ms,
        "error": error,
        **evaluated,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Records in ``path``. A corrupt line is skipped, not fatal."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            print(f"::warning::Skipping {path.name}:{lineno} (invalid JSON)")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), separators=(",", ":")) + "\n")


def sustained_failure(
    records: Sequence[Mapping[str, Any]], *, need: int = SUSTAINED_FAILURES
) -> bool:
    """True when the last ``need`` probes all have ``ok`` false."""
    if need < 1 or len(records) < need:
        return False
    return all(row.get("ok") is False for row in records[-need:])


def history_span_days(records: Sequence[Mapping[str, Any]]) -> float | None:
    """Days from the earliest ``probed_at`` to the latest. ``None`` if empty."""
    stamps = [
        parsed for row in records if (parsed := _parse_time(row.get("probed_at"))) is not None
    ]
    if not stamps:
        return None
    if len(stamps) == 1:
        return 0.0
    return (max(stamps) - min(stamps)).total_seconds() / 86400


def uptime_ratio(
    records: Sequence[Mapping[str, Any]], *, min_days: float = HISTORY_MIN_DAYS
) -> float | None:
    """Share of probes with ``ok`` true, once ``records`` span ``min_days``.

    ``None`` until then, so a caller cannot publish a figure from a short file.
    Stale-but-up probes count as up: freshness is not downtime.
    """
    span = history_span_days(records)
    if span is None or span < min_days:
        return None
    usable = [row for row in records if isinstance(row.get("ok"), bool)]
    if not usable:
        return None
    return sum(1 for row in usable if row["ok"]) / len(usable)


def _due(updated_at: datetime | None, now: datetime) -> bool:
    if updated_at is None:
        return True
    return now - updated_at >= ISSUE_UPDATE_INTERVAL


def health_issue_action(
    *,
    ok: bool,
    sustained: bool,
    issue_open: bool,
    updated_at: datetime | None,
    now: datetime,
) -> str | None:
    """``open``, ``comment``, ``recover``, or ``None`` for the uptime issue."""
    if ok:
        return "recover" if issue_open else None
    if not sustained:
        return None
    if not issue_open:
        return "open"
    if _due(updated_at, now):
        return "comment"
    return None


def stale_issue_action(
    *,
    stale: bool | None,
    issue_open: bool,
    updated_at: datetime | None,
    now: datetime,
) -> str | None:
    """``open``, ``comment``, ``recover``, or ``None`` for the freshness issue.

    ``stale is None`` (the probe could not read a build date) leaves an open
    issue alone: a down API is not evidence the index became fresh.
    """
    if stale is True:
        if not issue_open:
            return "open"
        if _due(updated_at, now):
            return "comment"
        return None
    if stale is False and issue_open:
        return "recover"
    return None


def _run(args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        shown = " ".join(args[:6])
        raise RuntimeError(f"{shown} exited {result.returncode}: {detail}")
    return result


def _checkout_history(repo: Path, dest: Path, branch: str) -> None:
    """Check ``branch`` out into ``dest``, or start an empty orphan branch.

    ``dest`` must not exist yet (``git worktree add`` creates it). A failed
    fetch means the remote branch is absent; the orphan tree contains none of
    ``repo``'s files, and the push is not forced, so a fetch outage cannot
    overwrite a branch that does exist.
    """
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch = subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin", refspec],
        check=False,
        capture_output=True,
        text=True,
    )
    if fetch.returncode == 0:
        _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), f"origin/{branch}"])
        _run(["git", "-C", str(dest), "checkout", "-B", branch])
        return
    _run(["git", "-C", str(repo), "worktree", "add", "--detach", str(dest), "HEAD"])
    _run(["git", "-C", str(dest), "checkout", "--orphan", branch])
    subprocess.run(["git", "-C", str(dest), "rm", "-rf", "."], check=False, capture_output=True)
    _run(["git", "-C", str(dest), "clean", "-fd"])


def _commit_record(dest: Path, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    readme = dest / "README.md"
    if not readme.exists():
        readme.write_text(MONITORING_README, encoding="utf-8")
    history_path = dest / HISTORY_FILE
    append_jsonl(history_path, record)
    _run(["git", "-C", str(dest), "add", "--", HISTORY_FILE, "README.md"])
    message = f"Record healthz probe {record.get('probed_at')}"
    _run(
        [
            "git",
            "-C",
            str(dest),
            "-c",
            "user.name=github-actions[bot]",
            "-c",
            "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit",
            "-m",
            message,
        ]
    )
    return load_jsonl(history_path)


def publish_history(
    repo: Path,
    record: Mapping[str, Any],
    *,
    branch: str = HISTORY_BRANCH,
    push: bool = True,
) -> list[dict[str, Any]]:
    """Append ``record`` on ``branch`` and, unless ``push`` is false, push it.

    Uses a worktree so the checkout that imported this module stays put. The
    push refspec is ``HEAD:refs/heads/<branch>`` and is never ``main``.
    """
    _run(["git", "-C", str(repo), "worktree", "prune"])
    parent = Path(tempfile.mkdtemp(prefix="umbra-healthz-"))
    dest = parent / "history"
    try:
        _checkout_history(repo, dest, branch)
        history = _commit_record(dest, record)
        if push:
            _run(["git", "-C", str(dest), "push", "origin", f"HEAD:refs/heads/{branch}"])
        return history
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(dest)],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(parent, ignore_errors=True)


def _open_issue(title: str, run: _Run) -> tuple[int | None, datetime | None]:
    result = run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "20",
            "--search",
            f"{title} in:title",
            "--json",
            "number,title,updatedAt",
        ]
    )
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        rows = []
    if not isinstance(rows, list):
        return None, None
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("title") == title
            and isinstance(row.get("number"), int)
        ):
            return row["number"], _parse_time(row.get("updatedAt"))
    return None, None


def apply_issue(
    action: str | None,
    *,
    title: str,
    body: str,
    run: _Run,
) -> None:
    """Open, comment on, or close the single open issue with ``title``."""
    if action is None:
        return
    number, _updated = _open_issue(title, run)
    if action == "open":
        if number is None:
            run(
                [
                    "gh",
                    "issue",
                    "create",
                    "--title",
                    title,
                    "--body",
                    body,
                    "--assignee",
                    HEALTH_ISSUE_ASSIGNEE,
                ]
            )
        else:
            run(["gh", "issue", "comment", str(number), "--body", body])
        return
    if number is None:
        return
    if action == "comment":
        run(["gh", "issue", "comment", str(number), "--body", body])
    elif action == "recover":
        run(
            [
                "gh",
                "issue",
                "close",
                str(number),
                "--reason",
                "completed",
                "--comment",
                body,
            ]
        )


def _run_url(env: Mapping[str, str]) -> str | None:
    run_id = env.get("GITHUB_RUN_ID")
    if not run_id:
        return None
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "reesehammer/umbra-py")
    return f"{server}/{repository}/actions/runs/{run_id}"


def _probe_timed_out(record: Mapping[str, Any]) -> bool:
    """True when the probe got no HTTP status because the client gave up."""
    if record.get("status_code") is not None:
        return False
    text = f"{record.get('error') or ''} {record.get('reason') or ''}".lower()
    return "timeout" in text or "timed out" in text


def failure_body(
    record: Mapping[str, Any], records: Sequence[Mapping[str, Any]], env: Mapping[str, str]
) -> str:
    streak = 0
    for row in reversed(records):
        if row.get("ok") is False:
            streak += 1
        else:
            break
    url = record.get("url", HEALTHZ_URL)
    if _probe_timed_out(record):
        lead = (
            f"@{HEALTH_ISSUE_ASSIGNEE} The external probe of {url} timed out "
            f"after {record.get('latency_ms')} ms and got no HTTP status. "
            "This is a timeout, not an HTTP error from the API."
        )
    else:
        lead = f"@{HEALTH_ISSUE_ASSIGNEE} The external probe of {url} failed."
    lines = [
        lead,
        "",
        f"- probed_at: {record.get('probed_at')}",
        f"- status: {record.get('status_code')}",
        f"- degraded: {record.get('degraded')}",
        f"- ready: {record.get('ready')}",
        f"- latency_ms: {record.get('latency_ms')}",
        f"- built_at: {record.get('built_at')}",
        f"- snapshot: {record.get('snapshot')}",
        f"- reason: {record.get('reason')}",
        f"- consecutive failures: {streak}",
        "",
        f"History is `{HISTORY_FILE}` on the `{HISTORY_BRANCH}` branch. "
        "`ok` in that file is the uptime bit; a stale index is a separate issue.",
    ]
    url = _run_url(env)
    if url:
        lines.extend(["", f"Run: {url}"])
    return "\n".join(lines) + "\n"


def recovery_body(record: Mapping[str, Any]) -> str:
    return (
        f"Recovered at {record.get('probed_at')}: status {record.get('status_code')}, "
        f"degraded {record.get('degraded')}, latency {record.get('latency_ms')} ms.\n"
    )


def stale_body(record: Mapping[str, Any], env: Mapping[str, str]) -> str:
    lines = [
        f"`built_at` is more than {INDEX_STALE_AFTER_DAYS} days old "
        "(or `/healthz` reported `stale: true`).",
        "",
        f"- probed_at: {record.get('probed_at')}",
        f"- built_at: {record.get('built_at')}",
        f"- snapshot: {record.get('snapshot')}",
        f"- stale: {record.get('stale')}",
        "",
        "The weekly publish runs Mondays 06:17 UTC. Search can still return "
        "200; this issue is freshness, not an outage. Uptime stays on the "
        f"`{HEALTH_ISSUE_TITLE}` issue and on `{HISTORY_FILE}`.",
    ]
    url = _run_url(env)
    if url:
        lines.extend(["", f"Run: {url}"])
    return "\n".join(lines) + "\n"


def fresh_body(record: Mapping[str, Any]) -> str:
    return (
        f"Index is fresh at {record.get('probed_at')}: built_at {record.get('built_at')}, "
        f"snapshot {record.get('snapshot')}.\n"
    )


def alert_from_history(
    history: Sequence[Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    run: _Run | None = None,
    now: datetime | None = None,
) -> None:
    """Open, update, or close the uptime and freshness issues."""
    if not history:
        return
    environ = os.environ if env is None else env
    runner = run or _run
    moment = now or datetime.now(timezone.utc)
    previous = history[-2] if len(history) > 1 else None
    if record.get("ok") is False or (previous and previous.get("ok") is False):
        number, updated = _open_issue(HEALTH_ISSUE_TITLE, runner)
        action = health_issue_action(
            ok=record.get("ok") is True,
            sustained=sustained_failure(history),
            issue_open=number is not None,
            updated_at=updated,
            now=moment,
        )
        body = (
            recovery_body(record) if action == "recover" else failure_body(record, history, environ)
        )
        apply_issue(action, title=HEALTH_ISSUE_TITLE, body=body, run=runner)
    if record.get("stale") is True or (previous and previous.get("stale") is True):
        number, updated = _open_issue(STALE_ISSUE_TITLE, runner)
        action = stale_issue_action(
            stale=record.get("stale") if isinstance(record.get("stale"), bool) else None,
            issue_open=number is not None,
            updated_at=updated,
            now=moment,
        )
        body = fresh_body(record) if action == "recover" else stale_body(record, environ)
        apply_issue(action, title=STALE_ISSUE_TITLE, body=body, run=runner)


def _note_history(history: Sequence[Mapping[str, Any]]) -> None:
    span = history_span_days(history)
    if span is None or span < HISTORY_MIN_DAYS:
        shown = "none" if span is None else f"{span:.2f} days"
        print(f"Uptime percentage withheld; history spans {shown} (need {HISTORY_MIN_DAYS} days).")
        return
    print(
        f"History spans {span:.2f} days. Uptime percentage stays withheld "
        "until a later report publishes it."
    )


def main(
    env: Mapping[str, str] | None = None,
    *,
    probe: Callable[[str], Mapping[str, Any]] | None = None,
    publish: Callable[[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None = None,
    alert: Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any]], None] | None = None,
) -> int:
    """Probe, record, alert, and return 1 when this probe is down or stale."""
    environ = os.environ if env is None else env
    url = environ.get("HEALTHZ_URL", HEALTHZ_URL)
    record = dict((probe or probe_once)(url))
    print(json.dumps(record), flush=True)
    if publish is None:
        repo = Path(environ.get("HEALTHZ_REPO", Path.cwd()))
        push = environ.get("HEALTHZ_PUSH", "1") != "0"
        history: Sequence[Mapping[str, Any]] = publish_history(repo, record, push=push)
    else:
        history = publish(record)
    if alert is None:
        alert_from_history(history, record, env=environ)
    else:
        alert(history, record)
    _note_history(history)
    if record.get("ok") is not True:
        print(
            f"::error title=Hosted API healthz::{record.get('reason')} "
            f"(latency {record.get('latency_ms')} ms)"
        )
    if record.get("stale") is True:
        print(
            f"::error title=Hosted index stale::built_at {record.get('built_at')} "
            f"is more than {INDEX_STALE_AFTER_DAYS} days old"
        )
    if record.get("ok") is not True or record.get("stale") is True:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
