"""The optional-dependency gate shared by every ``viz`` submodule.

``viz`` is the one part of the package that leans on heavy third-party
libraries (folium, rasterio, matplotlib, Pillow), all of them behind the
``viz`` extra. :func:`_require` is the single place that turns a missing one
into a :class:`~umbra_py.exceptions.MissingDependencyError` naming the install
command, so every render fails the same way rather than surfacing a bare
``ImportError`` from whichever line happened to import first.

Each submodule imports it by name, so ``monkeypatch.setattr`` on the *calling*
module is what stubs the check out in tests.
"""

from __future__ import annotations

from ..exceptions import MissingDependencyError


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - only without extra
        raise MissingDependencyError(
            f"'{module}' is required for interactive maps. "
            'Install the extra with: pip install "umbra-py[viz]"',
            hint='pip install "umbra-py[viz]"',
        ) from exc
