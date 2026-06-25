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
