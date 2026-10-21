from __future__ import annotations

from core.config import load_config


def test_prompt_refresh_defaults_to_on_and_can_be_disabled(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text(
        "retriever:\n  type: single\nreranker:\n  provider: infinity\nwebsearch:\n  provider: staan\n"
    )
    monkeypatch.delenv("PROMPTS_REFRESH_DEFAULTS", raising=False)
    assert load_config(config_path=tmp_path).prompts.refresh_defaults is True

    monkeypatch.setenv("PROMPTS_REFRESH_DEFAULTS", "false")
    assert load_config(config_path=tmp_path).prompts.refresh_defaults is False
