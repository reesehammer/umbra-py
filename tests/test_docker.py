"""Guards for the container entrypoint and the Railway first-deploy config.

The hosted STAC+MCP path is a pair of files a dashboard will override wrongly
if they drift: ``railway.toml``'s start command (Railway replaces the image
``ENTRYPOINT`` in exec form) and ``Dockerfile.mcp`` (the default image only
has ``[serve]``). Parsing them is enough — no Docker daemon, no Railway
account. Same spirit as ``test_mcp_registry.py``.
"""

from __future__ import annotations

import ast
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
        if line.startswith("CMD ["):
            continue
        # Public image hardcodes extras so a Railway build-arg cannot drop
        # FastAPI; the generic image keeps the ARG. The install line differs
        # on purpose.
        if line.startswith("RUN pip install"):
            continue
        lines.append(line)
    return lines


def test_railway_points_at_the_mcp_dockerfile():
    text = RAILWAY.read_text(encoding="utf-8")
    assert _toml_string(text, "dockerfilePath") == "Dockerfile.mcp"
    assert _toml_string(text, "healthcheckPath") == "/healthz"


def test_railway_start_command_hands_public_serve_to_the_entrypoint():
    """Bare ``serve`` is not on PATH; Railway would exec a missing binary."""
    cmd = _toml_string(RAILWAY.read_text(encoding="utf-8"), "startCommand")
    assert cmd != "serve"
    assert cmd != "mcp"
    assert "docker-entrypoint.sh" in cmd
    assert "serve --public" in cmd


def test_mcp_dockerfile_bakes_serve_and_mcp_and_public_cmd():
    """Extras are a literal pip install, not ARG -- a Railway UI build-arg of
    UMBRA_EXTRAS=mcp,viz would otherwise ship without FastAPI."""
    text = DOCKERFILE_MCP.read_text(encoding="utf-8")
    match = re.search(r'^RUN pip install "\.\[([^\]]+)\]"', text, re.MULTILINE)
    assert match, "Dockerfile.mcp must pip install extras as a literal list"
    extras = {part.strip() for part in match.group(1).split(",")}
    assert "mcp" in extras
    assert "serve" in extras
    assert "${UMBRA_EXTRAS}" not in text
    assert 'CMD ["serve", "--public"]' in text
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


def test_entrypoint_chowns_data_and_drops_root_before_umbra():
    """Railway Volumes are root-owned; fetch must not run as root or as a
    user who cannot mkdir /data/umbra-py."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    drop_at = text.index("os.setuid")
    assert "chown -R umbra:umbra" in text[:drop_at]
    assert drop_at < text.index("umbra index fetch")
    assert drop_at < text.index("umbra index fetch-thumbnails")
    assert drop_at < text.index("exec umbra")
    for path in (DOCKERFILE, DOCKERFILE_MCP):
        assert not re.search(r"^USER umbra\s*$", path.read_text(encoding="utf-8"), re.M), path
    snippet = re.search(
        r"exec python -c '(.*?)' /usr/local/bin/docker-entrypoint.sh",
        text,
        re.S,
    )
    assert snippet, "entrypoint must drop privileges with python -c"
    ast.parse(snippet.group(1))


def test_deploy_docs_do_not_advertise_bare_mcp_start_command():
    text = DEPLOY_DOCS.read_text(encoding="utf-8")
    assert "Dockerfile.mcp" in text
    assert "Start command:** `mcp`" not in text
    assert "railway.internal" in text
    assert "umbra serve --public" in text
    assert "/search" in text
    assert "pystac-client" in text or "pystac_client" in text
