"""Tests for LLM/VLM/embedder endpoint config (core.config.endpoints)."""

from __future__ import annotations

import pytest
from core.config import load_config
from core.config.endpoints import EmbedderConfig, LLMConfig, LLMParamsConfig, VLMConfig
from pydantic import ValidationError


def test_logprobs_is_a_configurable_knob():
    """logprobs stays a server-side knob operators can flip, shared by LLM/VLM."""
    assert "logprobs" in LLMParamsConfig.model_fields
    assert "logprobs" in LLMConfig.model_fields
    assert "logprobs" in VLMConfig.model_fields


def test_logprobs_defaults_off():
    """Default OFF: OpenRAG must not request logprobs on its own — sending them
    unsolicited breaks streaming on some OpenAI-compatible backends
    (linagora/openrag#563). Clients opt in per request.
    """
    assert LLMConfig().logprobs is False
    assert VLMConfig().logprobs is False
    assert LLMConfig().model_dump()["logprobs"] is False


@pytest.mark.parametrize(("provider", "expected"), [("", "vllm"), ("tei", "tei"), ("ollama", "ollama")])
def test_embedder_provider_comes_from_env(monkeypatch, tmp_path, provider, expected):
    (tmp_path / "config.yaml").write_text("retriever:\n  type: single\n", encoding="utf-8")
    # Empty is skipped by the override loop, like an unset variable.
    monkeypatch.setenv("EMBEDDER_PROVIDER", provider)

    assert load_config(config_path=tmp_path).embedder.provider == expected


def test_embedder_provider_openai_means_the_openai_compatible_client():
    """``openai`` was the field's default while nothing read it, so a config
    copied from that default must keep loading, as the vLLM client."""
    assert EmbedderConfig(provider="openai").provider == "vllm"


def test_embedder_provider_rejects_a_client_that_does_not_exist():
    with pytest.raises(ValidationError):
        EmbedderConfig(provider="infinity")
