from __future__ import annotations

from core.config import load_config


def test_postgres_provisioning_flags_can_be_overridden_from_env(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    monkeypatch.setenv("POSTGRES_AUTO_CREATE_DB", "false")
    monkeypatch.setenv("POSTGRES_RUN_MIGRATIONS", "false")

    settings = load_config(config_path=tmp_path)

    assert settings.rdb.auto_create_database is False
    assert settings.rdb.run_migrations is False
