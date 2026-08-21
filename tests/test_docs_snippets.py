"""Offline guards for Python fences in the README and docs_src/.

The notebooks already have this discipline (``tests/test_examples.py``). The
docs site and the landing-page README are the other copy-paste surface, and
they rot the same way: a renamed kwarg or a dropped export leaves a snippet
that CI never executes. These stdlib-only checks parse every `` ```python ``
fence and require every ``umbra_py`` name it imports to be public.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import umbra_py

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_SRC = REPO_ROOT / "docs_src"
README = REPO_ROOT / "README.md"

PUBLIC_NAMES = set(umbra_py.__all__)

_FENCE = re.compile(r"```(?:python|py)\n(.*?)```", re.DOTALL)

# Markdown files whose Python fences are part of the public copy-paste surface.
DOC_PAGES = (README, *sorted(DOCS_SRC.rglob("*.md")))


def _fences(path: Path) -> list[str]:
    return _FENCE.findall(path.read_text(encoding="utf-8"))


def test_doc_pages_exist():
    assert README.is_file()
    assert any(DOCS_SRC.rglob("*.md")), "no Markdown under docs_src/"


@pytest.mark.parametrize("path", DOC_PAGES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_python_fences_parse(path: Path):
    for i, source in enumerate(_fences(path), start=1):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise AssertionError(
                f"{path.relative_to(REPO_ROOT)} fence {i} does not parse: {exc}"
            ) from exc


@pytest.mark.parametrize("path", DOC_PAGES, ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_python_fences_only_use_public_api(path: Path):
    referenced: set[str] = set()
    aliases: set[str] = set()
    for source in _fences(path):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "umbra_py":
                for alias in node.names:
                    referenced.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "umbra_py":
                        aliases.add(alias.asname or "umbra_py")
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in aliases:
                    referenced.add(node.attr)

    unknown = sorted(name for name in referenced if name not in PUBLIC_NAMES)
    assert not unknown, (
        f"{path.relative_to(REPO_ROOT)} references non-public umbra_py names "
        f"{unknown}; either they were renamed or the snippet is wrong"
    )
