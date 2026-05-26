"""Pytest import guards for legacy top-level imports.

Some modules still import ``utils`` and the third-party ``openai`` package as
top-level names. During collection, pytest can prepend nested test directories
such as ``openrag/routers`` to ``sys.path``, where ``utils.py`` and
``openai.py`` would otherwise shadow those imports.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

OPENRAG_ROOT = Path(__file__).resolve().parent
root = str(OPENRAG_ROOT)
if root not in sys.path:
    sys.path.insert(0, root)

sys.modules.setdefault("utils", importlib.import_module("utils"))
sys.modules.setdefault("openai", importlib.import_module("openai"))
