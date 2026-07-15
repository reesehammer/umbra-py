"""Offline guards for the ``examples/*.ipynb`` gallery.

The notebooks hit Umbra's live bucket, so they can only *execute* under
``pytest -m network`` (see ``test_examples_execute`` at the bottom, gated on
``nbclient``). But their real failure mode is silent **drift**: a renamed
public symbol or a broken cell that nobody notices because CI never opens the
files. These stdlib-only checks (``json`` + ``ast``) run on every PR and make
that drift a red build — the notebooks stay honest without any network,
Jupyter, or heavy dependency.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

import umbra_py

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
NOTEBOOKS = sorted(EXAMPLES_DIR.glob("*.ipynb"))

# The CC-BY line the library propagates into every derived artifact; every
# notebook that shows the data must carry it, same discipline as the code.
ATTRIBUTION = umbra_py.ATTRIBUTION

# Public names an agent reading these notebooks is allowed to lean on. We check
# ``from umbra_py import X`` and ``umbra_py.X`` references against this so a
# renamed export can't leave a notebook quietly broken.
PUBLIC_NAMES = set(umbra_py.__all__)


def test_notebooks_exist():
    # The gallery is a deliverable; an empty glob means it was deleted or moved.
    assert NOTEBOOKS, "no example notebooks found under examples/"


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_is_well_formed(path: Path):
    nb = json.loads(path.read_text())
    assert nb.get("nbformat") == 4
    assert isinstance(nb.get("cells"), list) and nb["cells"], "notebook has no cells"
    for cell in nb["cells"]:
        assert cell["cell_type"] in {"markdown", "code"}
        assert isinstance(cell["source"], list)


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_outputs_are_cleared(path: Path):
    # Committed outputs bloat the repo and drift from the code that made them;
    # the notebooks ship clean and are executed on demand.
    nb = json.loads(path.read_text())
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            assert cell.get("outputs") == [], f"{path.name} has uncleared outputs"
            assert cell.get("execution_count") is None


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_code_cells_parse(path: Path):
    # Concatenating the code cells and parsing once catches any syntax error the
    # way a reader running the notebook top-to-bottom would hit it.
    nb = json.loads(path.read_text())
    source = "\n\n".join(
        "".join(cell["source"]) for cell in nb["cells"] if cell["cell_type"] == "code"
    )
    ast.parse(source)  # raises SyntaxError on a broken cell


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_only_uses_public_api(path: Path):
    """Every ``umbra_py`` symbol a notebook references must be public.

    Guards against the drift where a notebook imports a name we later rename or
    make private: the example would break for every reader, but no test would
    notice because CI never runs the notebook.
    """
    nb = json.loads(path.read_text())
    source = "\n\n".join(
        "".join(cell["source"]) for cell in nb["cells"] if cell["cell_type"] == "code"
    )
    tree = ast.parse(source)

    referenced: set[str] = set()
    aliases: set[str] = set()  # names bound to the umbra_py module itself
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
        f"{path.name} references non-public umbra_py names {unknown}; "
        "either they were renamed or the notebook is wrong"
    )


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.name)
def test_notebook_carries_attribution(path: Path):
    # License propagation is non-negotiable: the CC-BY line must appear in the
    # narrative of every notebook that renders or loads the data.
    text = path.read_text()
    assert ATTRIBUTION in text, f"{path.name} is missing the CC-BY attribution line"


@pytest.mark.network
def test_examples_execute(tmp_path):
    """Actually run every notebook end-to-end against the live bucket.

    Opt-in (``pytest -m network``) and gated on ``nbclient`` + the render
    extras, so it never burdens the core CI run — but when those are present it
    is a real, self-checking eval of the whole gallery: the notebooks assert
    their own results, so a green run proves the documented flows still work.
    """
    nbformat = pytest.importorskip("nbformat")
    from nbclient import NotebookClient  # noqa: PLC0415

    pytest.importorskip("rasterio")
    pytest.importorskip("xarray")

    for path in NOTEBOOKS:
        nb = nbformat.read(str(path), as_version=4)
        client = NotebookClient(nb, timeout=600, resources={"metadata": {"path": str(tmp_path)}})
        client.execute()
