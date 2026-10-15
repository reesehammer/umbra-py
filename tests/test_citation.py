"""Offline guards for CITATION.cff and the Zenodo citation metadata.

CITATION.cff is machine-readable citation metadata (Citation File Format 1.2.0)
that GitHub renders as a "Cite this repository" button. ``.zenodo.json`` is
what Zenodo actually copies onto an archived release when both files exist.
The version must track ``umbra_py.__version__`` so a citation names the release
it describes, and the DOI placeholder must stay in lockstep across the CFF
file, the README, and the docs page until a concept DOI replaces it.

Schema validation (``cffconvert --validate``) lives in the ``citation`` CI job,
not here: cffconvert 2.0.0 requires ``jsonschema<4``, and the schema tests in
this suite need ``jsonschema>=4.18``.
"""

from __future__ import annotations

import json
from pathlib import Path

from umbra_py import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
CITATION = REPO_ROOT / "CITATION.cff"
ZENODO = REPO_ROOT / ".zenodo.json"
README = REPO_ROOT / "README.md"
CITING = REPO_ROOT / "docs" / "citing.md"

DOI_PLACEHOLDER = "10.5281/zenodo.TBD"
# The copyright notice Umbra's terms require on adapted materials.
UMBRA_NOTICE = "SAR-derived data © [YEAR] Umbra Lab, Inc."


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
    assert _field(text, "doi") == DOI_PLACEHOLDER
    for key in ("message", "authors", "repository-code", "url", "references"):
        assert f"{key}:" in text, f"CITATION.cff is missing '{key}:'"
    assert "CC-BY-4.0" in text
    assert "https://umbra.space/open-data/" in text
    assert "family-names: Hammerstrom" in text


def test_citation_version_matches_package_version():
    # The whole point of the guard: a bumped __version__ must be reflected here.
    assert _field(CITATION.read_text(encoding="utf-8"), "version") == __version__


def test_zenodo_json_describes_the_software():
    meta = json.loads(ZENODO.read_text(encoding="utf-8"))
    assert meta["title"] == "umbra-py"
    assert meta["upload_type"] == "software"
    # Zenodo's license vocabulary id for the Apache License 2.0 in LICENSE.
    assert meta["license"] == "apache-2.0"
    assert meta["creators"] == [{"name": "Hammerstrom, Reese"}]
    assert "Umbra Space open data" in meta["description"]
    assert "CC BY 4.0" in meta["description"]
    for keyword in ("SAR", "Umbra", "STAC", "remote sensing"):
        assert keyword in meta["keywords"]
    identifiers = {item["identifier"] for item in meta["related_identifiers"]}
    assert identifiers == {
        "https://umbra-py.space",
        "https://umbra.space/open-data/",
    }


def test_citing_surfaces_share_the_placeholder_and_notice():
    readme = README.read_text(encoding="utf-8")
    citing = CITING.read_text(encoding="utf-8")
    nav = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "Citing: citing.md" in nav
    for text in (readme, citing):
        assert DOI_PLACEHOLDER in text
        assert UMBRA_NOTICE in text
        assert f"version = {{{__version__}}}" in text
        assert "© Umbra Space" in text
