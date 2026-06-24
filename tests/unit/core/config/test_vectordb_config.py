from __future__ import annotations

from core.config import load_config
from core.config.infrastructure import VectorDBConfig


def test_vectordb_config_timeout_default() -> None:
    # Replaces the value formerly hardcoded as ``MilvusVectorStore._timeout``.
    cfg = VectorDBConfig()
    assert cfg.timeout == 120.0
    assert isinstance(cfg.timeout, float)


def test_vdb_timeout_can_be_overridden_from_env(monkeypatch, tmp_path) -> None:
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("VDB_TIMEOUT", "200")

    settings = load_config(config_path=tmp_path)

    assert settings.vectordb.timeout == 200.0
