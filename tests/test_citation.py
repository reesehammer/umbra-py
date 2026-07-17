"""Offline guards for CITATION.cff.

CITATION.cff is machine-readable citation metadata (Citation File Format 1.2.0)
that GitHub renders as a "Cite this repository" button and that Zenodo and
citation managers consume. The one invariant that silently rots is the version:
it must track ``umbra_py.__version__`` so a citation names the release it
describes. These tests keep it honest with the standard library alone (no PyYAML
in the core ``[dev]`` env), mirroring the golden-file discipline in
``test_llms_txt.py``.
"""

from pathlib import Path

from umbra_py import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
CITATION = REPO_ROOT / "CITATION.cff"


def _field(text: str, key: str) -> str:
    """Return the value of a top-level ``key: value`` line (quotes stripped)."""
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip('"').strip("'")
    raise AssertionError(f"CITATION.cff has no top-level '{key}:' field")


def test_citation_file_exists():
    assert CITATION.is_file(), "CITATION.cff must live at the repository root"


def test_citation_has_the_required_cff_fields():
    text = CITATION.read_text(encoding="utf-8")
    # The four keys the Citation File Format 1.2.0 spec requires, plus the
    # metadata that makes the citation useful.
    assert _field(text, "cff-version") == "1.2.0"
    assert _field(text, "title") == "umbra-py"
    assert _field(text, "type") == "software"
    assert _field(text, "license") == "Apache-2.0"
    for key in ("message", "authors", "repository-code", "url"):
        assert f"{key}:" in text, f"CITATION.cff is missing '{key}:'"


def test_citation_version_matches_package_version():
    # The whole point of the guard: a bumped __version__ must be reflected here.
    assert _field(CITATION.read_text(encoding="utf-8"), "version") == __version__
