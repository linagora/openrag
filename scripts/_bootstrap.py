"""Import bootstrap for top-level developer scripts.

The application still uses bare imports such as ``from core...`` until the
future package-wide ``openrag.*`` import migration. Top-level scripts run from
outside the package, so they add the source package directory explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_openrag_source_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    package_root = repo_root / "openrag"
    for path in (repo_root, package_root):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
