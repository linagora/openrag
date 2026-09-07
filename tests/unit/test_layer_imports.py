"""Keep application modules from loading under two import roots."""

import pytest

from scripts import check_layer_imports as guard


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
