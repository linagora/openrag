from __future__ import annotations

from pathlib import Path

import pytest
from core.config import load_config


def test_language_detection_defaults_to_the_bundled_model_and_can_be_overridden(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n"
    )
    # Empty rather than unset: the loader ignores it, and load_dotenv() won't
    # replace it with a value from a developer's own .env.
    monkeypatch.setenv("LANGDETECT_MODEL", "")
    assert load_config(config_path=tmp_path).language_detection.model == "lite"

    monkeypatch.setenv("LANGDETECT_MODEL", "full")
    assert load_config(config_path=tmp_path).language_detection.model == "full"


def test_language_detection_rejects_an_unknown_model(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n"
    )
    monkeypatch.setenv("LANGDETECT_MODEL", "large")
    with pytest.raises(ValueError):
        load_config(config_path=tmp_path)


def test_shipped_config_uses_the_bundled_model(monkeypatch):
    # An explicit path bypasses the cached settings, which another test module
    # may have loaded under a different environment.
    monkeypatch.setenv("LANGDETECT_MODEL", "")
    shipped_conf_dir = Path(__file__).parents[4] / "conf"
    assert load_config(config_path=shipped_conf_dir).language_detection.model == "lite"
