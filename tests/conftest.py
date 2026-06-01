"""Pytest import guards for top-level imports.

During collection, pytest can prepend nested test directories such as
``tests/unit/api/routers`` to ``sys.path``, where local modules could otherwise shadow
third-party imports such as ``openai``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

OPENRAG_ROOT = Path(__file__).resolve().parents[1] / "openrag"
root = str(OPENRAG_ROOT)
if root not in sys.path:
    sys.path.insert(0, root)

sys.modules.setdefault("openai", importlib.import_module("openai"))
