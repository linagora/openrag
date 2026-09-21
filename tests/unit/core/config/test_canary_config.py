from __future__ import annotations

import pytest
from core.config import load_config
from core.config.canary import CanaryConfig
from pydantic import ValidationError


def _write_minimal_config(tmp_path):
    # The discriminated unions need their tag even when a developer .env sets
    # RERANKER_* / WEBSEARCH_* without a provider (load_config reads .env).
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n",
        encoding="utf-8",
    )


def test_canary_is_off_by_default(monkeypatch, tmp_path):
    # It writes a user, a partition and a document into the deployment: opt-in.
    _write_minimal_config(tmp_path)
    monkeypatch.delenv("CANARY_ENABLED", raising=False)

    assert load_config(config_path=tmp_path).canary.enabled is False


def test_canary_settings_come_from_env(monkeypatch, tmp_path):
    _write_minimal_config(tmp_path)
    monkeypatch.setenv("CANARY_ENABLED", "true")
    monkeypatch.setenv("CANARY_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("CANARY_INITIAL_DELAY_SECONDS", "5")
    monkeypatch.setenv("CANARY_INDEX_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("CANARY_REQUEST_TIMEOUT_SECONDS", "15")

    canary = load_config(config_path=tmp_path).canary

    assert canary == CanaryConfig(
        enabled=True,
        interval_seconds=300,
        initial_delay_seconds=5,
        index_timeout_seconds=120,
        request_timeout_seconds=15,
    )


def test_canary_interval_has_a_floor():
    # A sub-minute cadence would make the canary the busiest tenant of a quiet
    # deployment.
    with pytest.raises(ValidationError):
        CanaryConfig(interval_seconds=30)
