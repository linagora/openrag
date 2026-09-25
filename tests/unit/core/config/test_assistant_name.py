from core.config import load_config


def test_assistant_name_can_be_configured_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ASSISTANT_NAME", "Marianne")

    settings = load_config(config_path=tmp_path)

    assert settings.server.assistant_name == "Marianne"
