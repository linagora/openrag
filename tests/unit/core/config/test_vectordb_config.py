from __future__ import annotations

import pytest
from core.config import load_config
from core.config.infrastructure import VectorDBConfig
from pydantic import ValidationError


def test_vectordb_config_timeout_default() -> None:
    # Replaces the value formerly hardcoded as ``MilvusVectorStore._timeout``.
    cfg = VectorDBConfig()
    assert cfg.timeout == 120.0
    assert isinstance(cfg.timeout, float)


@pytest.mark.parametrize("bad_timeout", [0, -1, -0.5])
def test_vectordb_config_rejects_non_positive_timeout(bad_timeout: float) -> None:
    # A non-positive timeout must fail at config load time, not at client usage.
    with pytest.raises(ValidationError):
        VectorDBConfig(timeout=bad_timeout)


def test_vdb_timeout_can_be_overridden_from_env(monkeypatch, tmp_path) -> None:
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("VDB_TIMEOUT", "200")

    settings = load_config(config_path=tmp_path)

    assert settings.vectordb.timeout == 200.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"json_max_length": 0},
        {"json_max_length": -1},
        {"json_max_length": 2},
        {"json_max_length": 4096.5},
        {"json_safety_margin_bytes": 0},
        {"json_safety_margin_bytes": -1},
        {"json_safety_margin_bytes": True},
        {"json_max_length": 4096, "json_safety_margin_bytes": 4096},
        {"json_max_length": 4096, "json_safety_margin_bytes": 4095},
    ],
)
def test_vectordb_rejects_invalid_json_budgets(overrides) -> None:
    with pytest.raises(ValidationError):
        VectorDBConfig(**overrides)


@pytest.mark.parametrize("source", ["yaml", "env"])
def test_vectordb_json_budget_overrides(monkeypatch, tmp_path, source) -> None:
    monkeypatch.delenv("VDB_JSON_MAX_LENGTH", raising=False)
    monkeypatch.delenv("VDB_JSON_SAFETY_MARGIN_BYTES", raising=False)
    values = (32768, 512) if source == "yaml" else (49152, 1024)
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nvectordb:\n"
        f"  json_max_length: {values[0]}\n  json_safety_margin_bytes: {values[1]}\n",
        encoding="utf-8",
    )
    if source == "env":
        monkeypatch.setenv("VDB_JSON_MAX_LENGTH", "32768")
        monkeypatch.setenv("VDB_JSON_SAFETY_MARGIN_BYTES", "512")

    config = load_config(config_path=tmp_path).vectordb

    assert config.json_max_length == 32768
    assert config.json_safety_margin_bytes == 512
