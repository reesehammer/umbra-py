"""Guards for the container entrypoint and the Railway first-deploy config.

The hosted MCP path is a pair of files a dashboard will override wrongly if
they drift: ``railway.toml``'s start command (Railway replaces the image
``ENTRYPOINT`` in exec form) and ``Dockerfile.mcp`` (the default image only
has ``[serve]``). Parsing them is enough — no Docker daemon, no Railway
account. Same spirit as ``test_mcp_registry.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERFILE_MCP = REPO_ROOT / "Dockerfile.mcp"
ENTRYPOINT = REPO_ROOT / "docker-entrypoint.sh"
RAILWAY = REPO_ROOT / "railway.toml"
DEPLOY_DOCS = REPO_ROOT / "docs_src" / "deploy.md"


def _toml_string(text: str, key: str) -> str:
    match = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    assert match, f"railway.toml has no {key} ="
    return match.group(1)


def _instruction_body(path: Path) -> list[str]:
    """Dockerfile instructions, ignoring comments / extras ARG / MCP CMD."""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("ARG UMBRA_EXTRAS"):
            continue
        if line == 'CMD ["mcp"]':
            continue
        lines.append(line)
    return lines


def test_railway_points_at_the_mcp_dockerfile():
    text = RAILWAY.read_text(encoding="utf-8")
    assert _toml_string(text, "dockerfilePath") == "Dockerfile.mcp"
    assert _toml_string(text, "healthcheckPath") == "/healthz"


def test_railway_start_command_hands_mcp_to_the_entrypoint():
    """Bare ``mcp`` is not on PATH; Railway would exec a missing binary."""
    cmd = _toml_string(RAILWAY.read_text(encoding="utf-8"), "startCommand")
    assert cmd != "mcp"
    assert "docker-entrypoint.sh" in cmd
    assert cmd.endswith(" mcp'") or cmd.endswith(" mcp")


def test_mcp_dockerfile_bakes_mcp_extras_and_cmd():
    text = DOCKERFILE_MCP.read_text(encoding="utf-8")
    match = re.search(r"^ARG UMBRA_EXTRAS=(\S+)", text, re.MULTILINE)
    assert match, "Dockerfile.mcp must set ARG UMBRA_EXTRAS"
    extras = {part.strip() for part in match.group(1).split(",")}
    assert "mcp" in extras
    assert 'CMD ["mcp"]' in text
    assert "ENTRYPOINT" in text


def test_dockerfiles_stay_in_lockstep():
    assert _instruction_body(DOCKERFILE) == _instruction_body(DOCKERFILE_MCP)


def test_dockerfiles_do_not_declare_volume():
    """Railway's Metal builder rejects Dockerfile VOLUME; mount /data at run time."""
    for path in (DOCKERFILE, DOCKERFILE_MCP):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*VOLUME\b", text, re.MULTILINE), path


def test_entrypoint_treats_mcp_like_serve():
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert '[ "$1" = "mcp" ]' in text
    assert 'PORT="${PORT:-${UMBRA_PORT:-8000}}"' in text
    assert "umbra mcp --http" in text
    assert "XDG_CACHE_HOME" in text or "INDEX_DB" in text


def test_deploy_docs_do_not_advertise_bare_mcp_start_command():
    text = DEPLOY_DOCS.read_text(encoding="utf-8")
    assert "Dockerfile.mcp" in text
    assert "Start command:** `mcp`" not in text
    assert "railway.internal" in text
