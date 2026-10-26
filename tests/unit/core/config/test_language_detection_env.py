from __future__ import annotations

import pytest
from core.config import load_config


def test_language_detection_defaults_to_the_bundled_model_and_can_be_overridden(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n"
    )
    monkeypatch.delenv("LANGDETECT_MODEL", raising=False)
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
    monkeypatch.delenv("LANGDETECT_MODEL", raising=False)
    assert load_config().language_detection.model == "lite"
