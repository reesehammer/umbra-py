"""``python -m umbra_py.cli`` -- the module-execution entry the flat ``cli.py``
had before it became a package. Same behaviour as the ``umbra`` console script.
"""

from __future__ import annotations

from ._root import main

if __name__ == "__main__":
    main()
