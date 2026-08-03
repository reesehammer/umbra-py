"""Read the published JSON Schema contracts from an installed umbra-py.

``docs/schemas/`` is public API: every ``--json`` shape the CLI emits, every
document an ``umbra serve`` route returns and every payload an agent tool hands
back is described there, strictly, and checked against a real payload by
``tests/test_schemas.py``. What it was *not* was reachable — the files live in
``docs/``, so a wheel did not carry them and nothing in ``src/`` could load one.
A contract you can only read by cloning the repository is a document rather than
a dependency: a consumer could not validate against the version it installed,
and :mod:`umbra_py.serve` could not put the committed shape into the OpenAPI
document it generates, so its artifact routes described their responses as a
bare object while ``docs/schemas/`` described them exactly.

This module is that reach. The schemas keep one home — ``docs/schemas/``, which
is where the ``$id`` of every one of them says they live and where they are read
on GitHub — and the wheel carries a *copy* of that directory as package data
(``umbra_py/_schemas/``, via the ``force-include`` in ``pyproject.toml``), the
same way ``py.typed`` is a build artifact of a source-tree fact. So
:func:`schema_dir` looks for the packaged copy first and falls back to the
checkout the module was imported from, which is what makes an editable install
(the documented dev loop, and CI) resolve the same files a wheel ships.

Loading is stdlib only — these are data files, not a validator. Validating
against them needs ``jsonschema`` (a ``[dev]`` dependency), which is deliberately
the consumer's choice rather than a runtime requirement of the library.

    >>> from umbra_py.schemas import load_schema, schema_names
    >>> "stack-stats" in schema_names()
    True
    >>> load_schema("stack-stats")["title"]
    'Datacube statistics summary'
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_SUFFIX",
    "PACKAGE_DATA_DIR",
    "schema_dir",
    "schema_names",
    "schema_path",
    "load_schema",
]

#: Every published contract is ``<name>.schema.json``; the part before it is the
#: name callers use here (and the last path segment of the schema's ``$id``).
SCHEMA_SUFFIX = ".schema.json"

#: Where the wheel carries its copy of ``docs/schemas/``, relative to the
#: package root. Named here rather than spelled inline because
#: ``pyproject.toml``'s ``force-include`` has to agree with it, and
#: ``tests/test_schemas.py`` checks that the two do.
PACKAGE_DATA_DIR = "_schemas"


def _candidate_dirs() -> tuple[Path, ...]:
    """The directories that may hold the schemas, most authoritative first.

    The packaged copy wins: it is the one that belongs to *this* install, so a
    consumer validating against "the version I installed" gets exactly that. The
    checkout fallback is what an editable install resolves to, since a
    ``force-include`` only runs when a wheel is built.
    """
    package = Path(__file__).resolve().parent
    return (package / PACKAGE_DATA_DIR, package.parents[1] / "docs" / "schemas")


def schema_dir() -> Path:
    """The directory holding the published schemas.

    :raises FileNotFoundError: if neither the packaged copy nor a checkout is
        present, which means the install is missing its data files rather than
        that a particular schema is unknown.
    """
    for candidate in _candidate_dirs():
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "umbra-py's published JSON schemas are not installed; looked in "
        + ", ".join(str(path) for path in _candidate_dirs())
    )


def schema_names() -> tuple[str, ...]:
    """Every published schema's name, sorted (e.g. ``("chip-dataset", ...)``)."""
    return tuple(
        sorted(path.name[: -len(SCHEMA_SUFFIX)] for path in schema_dir().glob(f"*{SCHEMA_SUFFIX}"))
    )


def schema_path(name: str) -> Path:
    """The path of one published schema, by name (with or without the suffix)."""
    stem = name[: -len(SCHEMA_SUFFIX)] if name.endswith(SCHEMA_SUFFIX) else name
    path = schema_dir() / f"{stem}{SCHEMA_SUFFIX}"
    if not path.is_file():
        raise ValueError(
            f"No published schema named {stem!r}. Published: " + ", ".join(schema_names())
        )
    return path


def load_schema(name: str) -> dict[str, Any]:
    """Parse one published schema, by name (with or without the suffix).

    Returns a fresh object each call, so a caller that rewrites it for its own
    document (as :mod:`umbra_py.serve` does for OpenAPI) cannot change what the
    next caller reads.
    """
    return json.loads(schema_path(name).read_text(encoding="utf-8"))
