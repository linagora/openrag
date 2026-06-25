"""Tests for LLM/VLM endpoint config (core.config.endpoints)."""

from __future__ import annotations

from core.config.endpoints import LLMConfig, LLMParamsConfig, VLMConfig


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
