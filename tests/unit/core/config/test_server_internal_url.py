"""The workers' API base URL must follow the port uvicorn actually binds."""

from __future__ import annotations

from pathlib import Path

from core.config import load_config
from core.config.infrastructure import ServerConfig

# tests/unit/core/config/<this file> -> repository root
_CONF_DIR = Path(__file__).resolve().parents[4] / "conf"


def test_internal_url_defaults_to_the_container_internal_port(monkeypatch):
    """APP_iPORT is what entrypoint.sh passes to uvicorn, so a deployment that
    moves it must not leave workers calling 8080."""
    monkeypatch.setenv("APP_iPORT", "9000")
    assert ServerConfig().internal_url == "http://openrag:9000"


def test_internal_url_falls_back_to_8080(monkeypatch):
    monkeypatch.delenv("APP_iPORT", raising=False)
    assert ServerConfig().internal_url == "http://openrag:8080"


def test_an_empty_app_iport_falls_back_too(monkeypatch):
    """``APP_iPORT=`` in an env file is empty, not absent. entrypoint.sh and
    docker-compose.yaml both spell it ``${APP_iPORT:-8080}``, which falls back
    for either — so a bare assignment must not yield ``http://openrag:``."""
    monkeypatch.setenv("APP_iPORT", "")
    assert ServerConfig().internal_url == "http://openrag:8080"


def test_an_explicit_internal_url_wins(monkeypatch):
    """OPENRAG_INTERNAL_URL resolves onto this field, so an explicit value has
    to survive the default factory."""
    monkeypatch.setenv("APP_iPORT", "9000")
    assert ServerConfig(internal_url="http://api.internal:1234").internal_url == "http://api.internal:1234"


def test_the_shipped_config_does_not_pin_internal_url(monkeypatch):
    """Through the real loader and the real conf/config.yaml.

    A literal ``internal_url:`` re-added to the YAML would silently win over
    the default factory and pin workers to whatever port was written there —
    the exact regression this default exists to prevent. Only a load against
    the shipped file can catch it.
    """
    monkeypatch.setenv("APP_iPORT", "9999")
    monkeypatch.delenv("OPENRAG_INTERNAL_URL", raising=False)

    settings = load_config(config_path=_CONF_DIR)

    assert settings.server.internal_url == "http://openrag:9999"


def test_openrag_internal_url_overrides_the_derived_default(monkeypatch):
    monkeypatch.setenv("APP_iPORT", "9999")
    monkeypatch.setenv("OPENRAG_INTERNAL_URL", "https://api.internal")

    settings = load_config(config_path=_CONF_DIR)

    assert settings.server.internal_url == "https://api.internal"
