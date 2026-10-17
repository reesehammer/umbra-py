"""The Docker workflow retries an ECR Public 429, and only that failure.

A step timestamp such as ``#11 3.429 Downloading fastapi`` is not a rate
limit. After the ECR attempts are spent, the job passes
``PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim``. Dockerfiles are
unchanged; Railway builds ``deploy/Dockerfile.mcp``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "docker-build-ci.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker.yml"
MIRROR_PYTHON = "mirror.gcr.io/library/python:3.12-slim"
RATE_LIMIT = "429 Too Many Requests - toomanyrequests: Data limit exceeded"

FAKE_DOCKER = """\
#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

log = Path(os.environ["DOCKER_LOG"])
plan = Path(os.environ["DOCKER_PLAN"])
state = Path(os.environ["DOCKER_STATE"])
lines = [line for line in plan.read_text(encoding="utf-8").splitlines() if line]
n = int(state.read_text(encoding="utf-8") or "0") if state.exists() else 0
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if n >= len(lines):
    print(f"unexpected extra docker invocation: {sys.argv[1:]}", file=sys.stderr)
    sys.exit(99)
state.write_text(str(n + 1), encoding="utf-8")
code, _, msg = lines[n].partition(":")
if msg:
    print(msg, file=sys.stderr)
sys.exit(int(code))
"""


def _run(tmp_path: Path, plan: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "docker.plan").write_text(textwrap.dedent(plan).strip() + "\n", encoding="utf-8")
    (tmp_path / "docker.state").write_text("0", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DOCKER_LOG"] = str(tmp_path / "docker.log")
    env["DOCKER_PLAN"] = str(tmp_path / "docker.plan")
    env["DOCKER_STATE"] = str(tmp_path / "docker.state")
    env["UMBRA_BACKOFF_SECONDS"] = "0"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _calls(tmp_path: Path) -> list[list[str]]:
    text = (tmp_path / "docker.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def _limited(count: int) -> str:
    return "\n".join(f"1:{RATE_LIMIT}" for _ in range(count))


def test_rate_limit_retries_then_succeeds_without_the_mirror(tmp_path: Path):
    result = _run(tmp_path, _limited(2) + "\n0\n")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 3
    assert all(argv[:2] == ["build", "-f"] for argv in calls)
    assert all("PYTHON_IMAGE" not in " ".join(argv) for argv in calls)
    assert "attempt 1/5" in result.stderr
    assert "attempt 2/5" in result.stderr
    assert RATE_LIMIT in result.stdout


def test_timestamp_and_digest_are_not_a_rate_limit(tmp_path: Path):
    """`#11 3.429` and a sha256 that contains 429 must not be retried."""
    message = (
        "#11 3.429 Downloading fastapi-0.143.3-py3-none-any.whl"
        " sha256:a6e3429c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
    )
    result = _run(tmp_path, f"2:{message}\n")
    assert result.returncode == 2
    assert len(_calls(tmp_path)) == 1
    assert "3.429 Downloading fastapi" in result.stdout
    assert MIRROR_PYTHON not in result.stderr
    assert "rate-limited" not in result.stderr


def test_exhausted_ecr_retries_fall_back_to_the_mirror(tmp_path: Path):
    result = _run(tmp_path, _limited(5) + "\n0\n")
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 6
    assert all("PYTHON_IMAGE" not in " ".join(argv) for argv in calls[:5])
    assert f"PYTHON_IMAGE={MIRROR_PYTHON}" in " ".join(calls[5])
    assert f"building from {MIRROR_PYTHON}" in result.stderr


def test_mirror_error_keeps_its_exit_code(tmp_path: Path):
    result = _run(tmp_path, _limited(5) + "\n3:manifest unknown\n")
    assert result.returncode == 3
    assert len(_calls(tmp_path)) == 6


def test_both_registries_rate_limited_exits_1(tmp_path: Path):
    result = _run(tmp_path, _limited(8) + "\n")
    assert result.returncode == 1
    assert len(_calls(tmp_path)) == 8
    assert "Both public.ecr.aws and mirror.gcr.io rate-limited" in result.stderr


def test_workflow_is_plain_docker_build_with_no_cache():
    raw = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(raw)
    build = next(
        step
        for step in document["jobs"]["build"]["steps"]
        if step.get("name") == "Build image (serve extra)"
    )
    assert build["run"] == "bash .github/scripts/docker-build-ci.sh"
    assert "setup-buildx" not in raw
    assert "cache-from" not in raw
    assert "export-actions-env" not in raw
    script = SCRIPT.read_text(encoding="utf-8")
    assert "docker build -f deploy/Dockerfile" in script
    assert "cache-from" not in script
    assert "toomanyrequests|429 Too Many Requests" in script
    assert "Dockerfiles unchanged; Railway builds deploy/Dockerfile.mcp" in script
