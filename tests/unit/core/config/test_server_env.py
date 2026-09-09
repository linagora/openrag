from __future__ import annotations

from core.config import load_config


def _write_minimal_config(tmp_path):
    # The discriminated unions need their tag even when a developer .env sets
    # RERANKER_* / WEBSEARCH_* without a provider (load_config reads .env).
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n",
        encoding="utf-8",
    )


def test_metrics_token_defaults_to_none(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.delenv("METRICS_TOKEN", raising=False)

    settings = load_config(config_path=tmp_path)

    assert settings.server.metrics_token is None


def test_metrics_token_can_be_set_from_env(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("METRICS_TOKEN", "prom-scrape-secret")

    settings = load_config(config_path=tmp_path)

    assert settings.server.metrics_token == "prom-scrape-secret"


def test_blank_metrics_token_means_unset(monkeypatch, tmp_path):
    """``METRICS_TOKEN=`` in a .env must behave like an absent variable, not a
    token equal to the empty string (which would make every scrape 403)."""
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("METRICS_TOKEN", "   ")

    settings = load_config(config_path=tmp_path)

    assert settings.server.metrics_token is None
