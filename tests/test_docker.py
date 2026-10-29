"""Guards for the container entrypoint and the Railway first-deploy config.

The hosted STAC+MCP path is a pair of files a dashboard will override wrongly
if they drift: ``railway.toml``'s start command (Railway replaces the image
``ENTRYPOINT`` in exec form) and ``Dockerfile.mcp`` (the default image only
has ``[serve]``). Parsing them is enough — no Docker daemon, no Railway
account. Same spirit as ``test_mcp_registry.py``.

Dockerfiles / compose / entrypoint live under ``deploy/``; ``railway.toml``
stays at the repo root for default Railway discovery. Paths below are
repo-root-relative.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
DOCKERFILE = DEPLOY / "Dockerfile"
DOCKERFILE_MCP = DEPLOY / "Dockerfile.mcp"
# Docker publishes library/python on ECR Public under this tag.
ECR_PYTHON = "public.ecr.aws/docker/library/python:3.12-slim"
ENTRYPOINT = DEPLOY / "docker-entrypoint.sh"
COMPOSE = DEPLOY / "docker-compose.yml"
RAILWAY = REPO_ROOT / "railway.toml"
DEPLOY_DOCS = REPO_ROOT / "docs" / "deploy.md"


def _toml_string(text: str, key: str) -> str:
    match = re.search(rf'^{key}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    assert match, f"railway.toml has no {key} ="
    return match.group(1)


def _instruction_body(path: Path) -> list[str]:
    """Dockerfile instructions, ignoring comments / extras ARG / MCP CMD.

    The hosted image's hashed install is a continued ``RUN pip install`` plus
    a ``COPY`` of the lock. Those lines are the difference from the self-host
    image, whose extras stay a build-arg. A ``HEALTHCHECK`` command line
    starts with ``CMD [`` and stays ignored, as before.
    """
    lines: list[str] = []
    skipping_pip = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        continued = line.endswith("\\")
        if skipping_pip:
            skipping_pip = continued
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith("ARG UMBRA_EXTRAS"):
            continue
        if line.startswith("CMD ["):
            continue
        if line.startswith("RUN pip install"):
            skipping_pip = continued
            continue
        if line.startswith("COPY deploy/requirements.txt") or line.startswith(
            "COPY deploy/build-requirements.txt"
        ):
            continue
        lines.append(line)
    return lines


def _python_image_pin(path: Path) -> str:
    line = next(
        item
        for item in path.read_text(encoding="utf-8").splitlines()
        if item.startswith("ARG PYTHON_IMAGE=")
    )
    return line.split("=", 1)[1].strip()


def test_railway_points_at_the_mcp_dockerfile():
    text = RAILWAY.read_text(encoding="utf-8")
    assert _toml_string(text, "dockerfilePath") == "deploy/Dockerfile.mcp"
    assert _toml_string(text, "healthcheckPath") == "/livez"


def test_railway_lives_at_repo_root_not_under_deploy():
    assert RAILWAY.is_file()
    assert not (DEPLOY / "railway.toml").exists()


def test_compose_pins_project_name_and_root_build_context():
    """Relocating compose under deploy/ must not rename the project volume."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert re.search(r"^name:\s*umbra-py\s*$", text, re.MULTILINE)
    assert re.search(r"^\s*context:\s*\.\.\s*$", text, re.MULTILINE)
    assert re.search(r"^\s*dockerfile:\s*deploy/Dockerfile\s*$", text, re.MULTILINE)


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
    match = re.search(r'pip install --no-deps --no-build-isolation "\.\[([^\]]+)\]"', text)
    assert match, "Dockerfile.mcp must install the package without build isolation"
    extras = {part.strip() for part in match.group(1).split(",")}
    assert extras == {"mcp", "viz", "serve"}
    assert "--require-hashes" in text
    assert "deploy/requirements.txt" in text
    assert "deploy/build-requirements.txt" in text
    assert "pip check" not in text
    assert "PYTHON_IMAGE" in text
    assert "${UMBRA_EXTRAS}" not in text
    assert 'CMD ["serve", "--public"]' in text
    assert "ENTRYPOINT" in text


def test_dockerfiles_stay_in_lockstep():
    assert _instruction_body(DOCKERFILE) == _instruction_body(DOCKERFILE_MCP)


def test_dockerfiles_pull_official_python_from_ecr_public():
    """Anonymous Docker Hub pulls 429 and time out. Both images default to the
    ECR Public copy of official python:3.12-slim, pinned by digest."""
    pins = []
    for path in (DOCKERFILE, DOCKERFILE_MCP):
        text = path.read_text(encoding="utf-8")
        pin = _python_image_pin(path)
        assert pin.startswith(f"{ECR_PYTHON}@sha256:")
        digest = pin.rsplit("@", 1)[1]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), pin
        pins.append(pin)
        assert "FROM ${PYTHON_IMAGE} AS runtime" in text
        assert not re.search(r"^FROM python:", text, re.MULTILINE), path
    assert pins[0] == pins[1]


def test_deploy_docs_quote_the_same_digest():
    pin = _python_image_pin(DOCKERFILE)
    text = DEPLOY_DOCS.read_text(encoding="utf-8")
    assert pin in text


def test_hosted_lock_is_hashed_wheels_for_the_deployed_extras():
    """The lock omits this package (installed with --no-deps) and pins
    requests at or above the pyproject floor."""
    text = (DEPLOY / "requirements.txt").read_text(encoding="utf-8")
    assert "--python-version 3.12" in text
    assert "--python-platform x86_64-manylinux_2_28" in text
    assert "--generate-hashes" in text
    assert "--no-emit-package umbra-py" in text
    assert "--only-binary :all:" in text
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("--hash"):
            current.append(line)
            continue
        if current:
            blocks.append(current)
        current = [line]
    if current:
        blocks.append(current)
    assert blocks
    names = set()
    for block in blocks:
        blob = "\n".join(block)
        assert "--hash=sha256:" in blob, block[0]
        assert ".tar.gz" not in blob
        name, version = block[0].split("==", 1)
        version = version.split("\\", 1)[0].strip()
        names.add(name)
        if name == "requests":
            parts = tuple(int(piece) for piece in version.split("."))
            assert parts >= (2, 33, 0), version
    for required in ("fastapi", "uvicorn", "mcp", "rasterio", "matplotlib", "folium"):
        assert required in names
    assert "umbra-py" not in names


def _toml_array(text: str, key: str) -> str:
    """Body of a top-level ``key = [ ... ]`` array, respecting quoted brackets."""
    match = re.search(rf"^{re.escape(key)}\s*=\s*\[", text, re.M)
    assert match, key
    depth = 1
    in_str = False
    out: list[str] = []
    i = match.end()
    while i < len(text) and depth:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\":
                i += 1
                if i < len(text):
                    out.append(text[i])
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch == "[":
            depth += 1
            out.append(ch)
        elif ch == "]":
            depth -= 1
            if depth:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    assert depth == 0, key
    return "".join(out)


def _requirement_name(spec: str) -> str:
    spec = spec.split(";", 1)[0].strip()
    spec = spec.split("[", 1)[0]
    name = re.split(r"(>=|<=|==|~=|!=|>|<)", spec, maxsplit=1)[0].strip()
    return name.replace("_", "-").lower()


def _lock_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or line.startswith("#") or not line.strip():
            continue
        name = line.split("==", 1)[0]
        names.add(name.replace("_", "-").lower())
    return names


def test_hosted_lock_pins_every_direct_hosted_requirement():
    """Core deps plus the mcp, viz, and serve extras must be in the lock.

    The image installs the package with --no-deps, so a name that is only in
    pyproject.toml is absent at runtime.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    specs: list[str] = []
    for key in ("dependencies", "mcp", "viz", "serve"):
        specs.extend(re.findall(r'"([^"]+)"', _toml_array(text, key)))
    wanted = {_requirement_name(spec) for spec in specs} - {"umbra-py"}
    locked = _lock_names(DEPLOY / "requirements.txt")
    missing = sorted(wanted - locked)
    assert not missing, missing
    assert {"requests", "click", "defusedxml", "mcp", "fastapi", "uvicorn"} <= wanted


def test_build_lock_pins_hatchling():
    text = (DEPLOY / "build-requirements.txt").read_text(encoding="utf-8")
    assert "hatchling==" in text
    assert "--generate-hashes" in text
    assert "--only-binary :all:" in text
    assert ".tar.gz" not in text
    assert "--hash=sha256:" in text


def test_requests_floor_is_the_patched_release():
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Core dependency and the ai extra, which repeats the floor.
    assert text.count('"requests>=2.33.0"') == 2


def test_deploy_docs_name_the_ecr_python_image():
    text = DEPLOY_DOCS.read_text(encoding="utf-8")
    assert ECR_PYTHON in text
    assert "PYTHON_IMAGE" in text


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
    assert "deploy/Dockerfile.mcp" in text
    assert "Start command:** `mcp`" not in text
    assert "railway.internal" in text
    assert "umbra serve --public" in text
    assert "/search" in text
    assert "pystac-client" in text or "pystac_client" in text


def test_entrypoint_refreshes_the_index_on_every_boot():
    """PRD P1-1: a persistent volume kept catalog.db across deploys and the
    entrypoint only fetched when the file was missing, so the hosted API stayed
    on its first snapshot. Every boot must now run the change-aware refresh,
    and a failed refresh with an index present must keep serving it."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "umbra index fetch --if-changed" in text
    assert '[ ! -f "$INDEX_DB" ]; then\n    echo "No catalog index' not in text
    assert "serving the existing index" in text


def test_entrypoint_logs_free_space_before_refreshing():
    """The Oct 9 incident filled the /data volume and nothing in the boot log
    said what with; free space and the largest entries come first now."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "df -Ph" in text and "du -xah" in text
    assert text.index("du -xah") < text.index("umbra index fetch --if-changed")


def test_entrypoint_refreshes_the_thumbnail_sidecar_on_every_boot():
    """Same volume, same bug: the sidecar was fetched only when missing, so a
    stale catalog.thumbs.db was never replaced (or re-merged into a refreshed
    index). A failed fetch must not stop the server from starting."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "umbra index fetch-thumbnails --if-changed ||" in text
    assert '[ ! -f "$THUMBS_DB" ]' not in text
    assert text.index("umbra index fetch --if-changed") < text.index(
        "umbra index fetch-thumbnails --if-changed"
    )


def test_entrypoint_thumbnail_fetch_uses_the_manifest_asset_name():
    """The boot command must download the asset the publish workflow uploads.

    ``fetch-thumbnails`` with no ``--url`` reads ``catalog.thumbs.manifest.json``
    and reassembles ``catalog.thumbs.db``. A hardcoded ``--url`` of the legacy
    single asset would 404 once that file is no longer published.
    """
    from umbra_py.constants import CATALOG_THUMBS_MANIFEST_ASSET

    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert CATALOG_THUMBS_MANIFEST_ASSET in text
    fetch_line = next(line for line in text.splitlines() if "fetch-thumbnails" in line)
    assert "--url" not in fetch_line
    assert "--force" not in fetch_line
    assert "fewer previews" in text
