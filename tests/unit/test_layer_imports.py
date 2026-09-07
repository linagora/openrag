"""Keep application modules from loading under two import roots."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_layer_imports", Path(__file__).resolve().parents[2] / "scripts/check_layer_imports.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    ("source", "rejected"),
    [
        ("from openrag.core.utils.exceptions import OpenRAGError", True),
        ("import openrag.core.utils.exceptions", True),
        ("from openrag import core", True),
        ("from core.utils.exceptions import OpenRAGError", False),
        ("from .exceptions import OpenRAGError", False),
    ],
)
def test_canonical_import_root(tmp_path, monkeypatch, source, rejected):
    monkeypatch.setattr(guard, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(guard, "OPENRAG", tmp_path / "openrag")
    path = tmp_path / "openrag/core/utils/example.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)

    assert bool(guard.check_file(path)) is rejected
