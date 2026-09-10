from __future__ import annotations

from core.config import load_config


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
