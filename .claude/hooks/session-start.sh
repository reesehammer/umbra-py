#!/bin/bash
# SessionStart hook for Claude Code on the web (and other remote coding agents).
#
# A fresh remote container has `uv` and the CLI linters on PATH but does NOT have
# umbra-py itself installed, so `pytest`, `mypy`, and the `umbra` CLI all fail
# until the package is installed editable. This hook closes that gap so an agent
# can run the exact checks CI runs (see .github/workflows/ci.yml) from the first
# turn — no "command not found", no import-skipped tests.
#
# It installs every extra so the FULL offline suite runs rather than
# import-skipping the viz / serve / convert / load / agent modules — mirroring
# CI's `test-all-extras` job, the one place every module actually executes and
# where the coverage gate is measured.
#
# Runs synchronously (no `{"async": true}` line) so the environment is ready
# before the first agent turn — preventing a race where a check runs before its
# dependencies exist. Idempotent and non-interactive: safe to re-run on
# resume/clear/compact, and `uv` no-ops when nothing changed.
set -euo pipefail

# Only bother in a remote environment (Claude Code on the web). Local sessions
# manage their own venv per AGENTS.md §3.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# `--system` matches CI: install into the container's Python rather than a venv.
# The extras list mirrors ci.yml's `test-all-extras` job so nothing import-skips.
uv pip install --system -e ".[dev,all,mcp,serve,ai,langchain,llamaindex]"

echo "umbra-py installed editable with all extras — ruff / mypy / pytest ready." >&2
