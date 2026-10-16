"""The Docker workflow must outlive an anonymous ECR Public 429.

The failure mode is BuildKit's ``load metadata`` for
``public.ecr.aws/docker/library/python:3.12-slim`` returning
``429 Too Many Requests - toomanyrequests: Data limit exceeded``. A later
empty commit used to clear it by landing on another runner. The workflow
retries that build and, only after the retries are spent, passes
``PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim``. Railway builds the
Dockerfile itself and does not pass that arg, so the default stays ECR Public.
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
EXPORTER = REPO_ROOT / ".github" / "actions" / "export-actions-env" / "index.js"
ECR_PYTHON = "public.ecr.aws/docker/library/python:3.12-slim"
MIRROR_PYTHON = "mirror.gcr.io/library/python:3.12-slim"
BUILDKIT_MIRROR = "mirror.gcr.io/moby/buildkit:buildx-stable-1"

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
spec = lines[n]
code, _, msg = spec.partition(":")
if msg:
    print(msg, file=sys.stderr)
sys.exit(int(code))
"""


def _install_fake(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "docker.log").write_text("", encoding="utf-8")
    (tmp_path / "docker.state").write_text("0", encoding="utf-8")
    return bin_dir


def _run(tmp_path: Path, plan: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _install_fake(tmp_path)
    (tmp_path / "docker.plan").write_text(textwrap.dedent(plan).strip() + "\n", encoding="utf-8")
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


def _invocations(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line]


def _rate_limit(count: int) -> str:
    return "\n".join(["10:429 Too Many Requests - toomanyrequests: Data limit exceeded"] * count)


def test_first_build_uses_gha_cache_and_the_dockerfile_default(tmp_path: Path):
    result = _run(tmp_path, "0\n")
    assert result.returncode == 0, result.stderr
    calls = _invocations(tmp_path)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:2] == ["buildx", "build"]
    assert "--cache-from" in argv and "type=gha" in argv
    assert "--cache-to" in argv and "type=gha,mode=max,ignore-error=true" in argv
    assert "--build-arg" in argv and "UMBRA_EXTRAS=serve" in argv
    assert "--load" in argv
    assert "PYTHON_IMAGE" not in " ".join(argv)
    assert ECR_PYTHON not in " ".join(argv)
    assert MIRROR_PYTHON not in " ".join(argv)


def test_rate_limit_retries_then_succeeds_without_the_mirror(tmp_path: Path):
    result = _run(tmp_path, _rate_limit(2) + "\n0\n")
    assert result.returncode == 0, result.stderr
    calls = _invocations(tmp_path)
    assert len(calls) == 3
    assert all("PYTHON_IMAGE" not in " ".join(argv) for argv in calls)
    assert "attempt 1/5" in result.stderr
    assert "attempt 2/5" in result.stderr


def test_non_rate_limit_failure_is_not_retried_or_mirrored(tmp_path: Path):
    result = _run(tmp_path, "2:pip install failed\n")
    assert result.returncode == 2
    assert len(_invocations(tmp_path)) == 1
    assert MIRROR_PYTHON not in result.stderr


def test_exhausted_ecr_retries_fall_back_to_the_gcr_mirror(tmp_path: Path):
    result = _run(tmp_path, _rate_limit(5) + "\n0\n")
    assert result.returncode == 0, result.stderr
    calls = _invocations(tmp_path)
    assert len(calls) == 6
    ecr_calls, mirror_calls = calls[:5], calls[5:]
    assert all("PYTHON_IMAGE" not in " ".join(argv) for argv in ecr_calls)
    mirror = " ".join(mirror_calls[0])
    assert f"PYTHON_IMAGE={MIRROR_PYTHON}" in mirror
    assert "--cache-from" in mirror_calls[0]
    assert f"building from {MIRROR_PYTHON}" in result.stderr.lower()


def test_mirror_rate_limit_retries_then_a_real_mirror_error_stops(tmp_path: Path):
    plan = _rate_limit(5) + "\n" + _rate_limit(1) + "\n3:manifest unknown\n"
    result = _run(tmp_path, plan)
    assert result.returncode == 3
    calls = _invocations(tmp_path)
    assert len(calls) == 7
    assert sum(f"PYTHON_IMAGE={MIRROR_PYTHON}" in " ".join(argv) for argv in calls) == 2


def test_both_registries_rate_limited_fails(tmp_path: Path):
    result = _run(tmp_path, _rate_limit(5) + "\n" + _rate_limit(3) + "\n")
    assert result.returncode == 1
    assert len(_invocations(tmp_path)) == 8
    assert "Both public.ecr.aws and mirror.gcr.io rate-limited" in result.stderr


def test_script_defaults_and_workflow_wiring():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "public.ecr.aws/docker/library/python:3.12-slim" in script
    assert "mirror.gcr.io/library/python:3.12-slim" in script
    assert "UMBRA_ECR_ATTEMPTS:-5" in script
    assert "UMBRA_MIRROR_ATTEMPTS:-3" in script

    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = document["jobs"]["build"]["steps"]
    uses = [step.get("uses", "") for step in steps]
    assert "docker/setup-buildx-action@v4" in uses
    assert "./.github/actions/export-actions-env" in uses
    setup = next(step for step in steps if step.get("uses") == "docker/setup-buildx-action@v4")
    assert setup["with"]["driver-opts"] == f"image={BUILDKIT_MIRROR}"
    build = next(step for step in steps if step.get("name") == "Build image (serve extra)")
    assert build["run"] == "bash .github/scripts/docker-build-ci.sh"
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "docker build -f" not in raw
    assert "docker.io" not in raw
    assert "PYTHON_IMAGE" not in raw


def test_export_action_writes_cache_env_without_printing_the_token(tmp_path: Path):
    dest = tmp_path / "github.env"
    dest.write_text("", encoding="utf-8")
    token = "runtime-token-do-not-print"
    env = os.environ.copy()
    env.update(
        {
            "GITHUB_ENV": str(dest),
            "GITHUB_ACTIONS": "true",
            "ACTIONS_RUNTIME_TOKEN": token,
            "ACTIONS_RESULTS_URL": "https://results.example/cache",
            "ACTIONS_CACHE_SERVICE_V2": "true",
        }
    )
    result = subprocess.run(
        ["node", str(EXPORTER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout
    assert token not in result.stderr
    assert "ACTIONS_RUNTIME_TOKEN" in result.stdout
    written = dest.read_text(encoding="utf-8")
    assert f"ACTIONS_RUNTIME_TOKEN<<EOF_ACTIONS_RUNTIME_TOKEN\n{token}\n" in written
    assert (
        "ACTIONS_RESULTS_URL<<EOF_ACTIONS_RESULTS_URL\nhttps://results.example/cache\n" in written
    )
    assert "ACTIONS_CACHE_SERVICE_V2<<EOF_ACTIONS_CACHE_SERVICE_V2\ntrue\n" in written


def test_export_action_fails_in_gha_when_the_cache_token_is_missing(tmp_path: Path):
    dest = tmp_path / "github.env"
    dest.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["GITHUB_ENV"] = str(dest)
    env["GITHUB_ACTIONS"] = "true"
    env.pop("ACTIONS_RUNTIME_TOKEN", None)
    result = subprocess.run(
        ["node", str(EXPORTER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "ACTIONS_RUNTIME_TOKEN" in result.stderr
