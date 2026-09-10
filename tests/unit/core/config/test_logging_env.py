from __future__ import annotations

import pytest
from core.config import load_config
from pydantic import ValidationError


def _write_minimal_config(tmp_path):
    # Discriminated unions need their tag even when a developer .env sets
    # RERANKER_* / WEBSEARCH_* without a provider (load_config reads .env).
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n",
        encoding="utf-8",
    )


def test_log_dir_is_no_longer_a_setting(monkeypatch, tmp_path):
    """A leftover LOG_DIR in a .env is ignored silently, not an error."""
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("LOG_DIR", "/somewhere")

    settings = load_config(config_path=tmp_path)

    assert "log_dir" not in settings.paths.keys()


def test_log_format_defaults_to_text(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    settings = load_config(config_path=tmp_path)

    assert settings.verbose.format == "text"


def test_log_format_json_from_env(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = load_config(config_path=tmp_path)

    assert settings.verbose.format == "json"


def test_log_format_rejects_unknown_value(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("LOG_FORMAT", "yaml")

    with pytest.raises(ValidationError):
        load_config(config_path=tmp_path)


def test_log_format_is_case_insensitive(monkeypatch, tmp_path):
    """``LOG_FORMAT=JSON`` in a .env must not fail validation at import."""
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("LOG_FORMAT", "JSON")

    settings = load_config(config_path=tmp_path)

    assert settings.verbose.format == "json"
