from __future__ import annotations

from core.config import load_config


def test_postgres_provisioning_flags_can_be_overridden_from_env(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_AUTO_CREATE_DB", "false")
    monkeypatch.setenv("POSTGRES_RUN_MIGRATIONS", "false")

    settings = load_config(config_path=tmp_path)

    assert settings.rdb.auto_create_database is False
    assert settings.rdb.run_migrations is False


def test_llm_and_vlm_enable_thinking_can_be_overridden_from_env(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    monkeypatch.setenv("VLM_ENABLE_THINKING", "true")
    monkeypatch.setenv("OPENAI_LOADER_ENABLE_THINKING", "false")

    settings = load_config(config_path=tmp_path)

    assert settings.llm.enable_thinking is False
    assert settings.vlm.enable_thinking is True
    assert settings.loader.openai.enable_thinking is False


def test_vlm_timeout_can_be_overridden_independently_of_llm_timeout(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("VLM_TIMEOUT", "180")

    settings = load_config(config_path=tmp_path)

    assert settings.vlm.timeout == 180.0
    assert settings.llm.timeout == 60
