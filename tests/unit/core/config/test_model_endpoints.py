"""Tests for ModelsConfig frozen-field + mutable-dict invariant."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.config.model_endpoints import (
    LLM_CONTEXT_SIZE_KEY,
    LLM_OUTPUT_TOKENS_KEY,
    ModelEndpointConfig,
    ModelEndpointRow,
    ModelsConfig,
)
from pydantic import ValidationError


def _row_payload(**overrides):
    """Build a valid model endpoint row payload with targeted overrides."""
    now = datetime.now(UTC)
    payload = {
        "name": "default",
        "model_type": "llm",
        "endpoint": "http://vllm:8000/v1",
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


def test_models_config_dicts_are_mutable_in_place():
    """Services perform clear()+update() atomic swaps on frozen ModelsConfig fields."""
    cfg = ModelsConfig()
    ep = ModelEndpointConfig(endpoint="http://vllm:8000/v1")

    cfg.embedder["default"] = ep
    assert "default" in cfg.embedder

    new_ep = ModelEndpointConfig(endpoint="http://new:8000/v1")
    cfg.embedder.clear()
    cfg.embedder.update({"v2": new_ep})
    assert list(cfg.embedder) == ["v2"]


def test_models_config_field_reassignment_raises():
    """Frozen ConfigMixin prevents field reassignment — only in-place mutation works."""
    cfg = ModelsConfig()
    with pytest.raises((TypeError, ValidationError)):
        cfg.embedder = {}  # type: ignore[misc]


def test_model_endpoint_row_invalid_model_type():
    """Persisted endpoint rows reject unknown model endpoint types."""
    with pytest.raises(ValidationError):
        ModelEndpointRow(**_row_payload(model_type="not-a-model"))


@pytest.mark.parametrize("batch_size", [0, -1])
def test_model_endpoint_row_non_positive_batch_size(batch_size: int):
    """Persisted endpoint rows reject non-positive batch sizes."""
    with pytest.raises(ValidationError):
        ModelEndpointRow(**_row_payload(batch_size=batch_size))


@pytest.mark.parametrize("timeout", [0, -0.1])
def test_model_endpoint_row_non_positive_timeout(timeout: float):
    """Persisted endpoint rows reject non-positive timeouts."""
    with pytest.raises(ValidationError):
        ModelEndpointRow(**_row_payload(timeout=timeout))


# --------------------------------------------------------------------------- #
# Named-LLM-endpoint token budget accessors
# --------------------------------------------------------------------------- #


def _with_default_llm(**extra) -> ModelsConfig:
    cfg = ModelsConfig()
    cfg.llm["default"] = ModelEndpointConfig(endpoint="http://vllm:8000/v1", extra=dict(extra))
    return cfg


def test_llm_token_budgets_none_when_unregistered():
    cfg = ModelsConfig()
    assert cfg.llm_extra() == {}
    assert cfg.llm_context_size() is None
    assert cfg.llm_output_tokens() is None


def test_llm_token_budgets_read_from_extra():
    cfg = _with_default_llm(**{LLM_CONTEXT_SIZE_KEY: 32768, LLM_OUTPUT_TOKENS_KEY: 2048})
    assert cfg.llm_context_size() == 32768
    assert cfg.llm_output_tokens() == 2048


def test_llm_token_budgets_none_when_key_absent():
    cfg = _with_default_llm(implementation="vllm")  # unrelated extra key
    assert cfg.llm_context_size() is None
    assert cfg.llm_output_tokens() is None


@pytest.mark.parametrize("bad", [0, -1, "not-a-number", None, 12.5])
def test_llm_token_budgets_coerce_invalid_to_none(bad):
    # A non-positive / non-int stored value means "no override" — the preflight
    # falls back to the global default rather than trusting garbage.
    cfg = _with_default_llm(**{LLM_CONTEXT_SIZE_KEY: bad, LLM_OUTPUT_TOKENS_KEY: bad})
    assert cfg.llm_context_size() is None
    assert cfg.llm_output_tokens() is None


def test_llm_token_budgets_resolved_by_name():
    # A partition's chat_llm preset names a catalogued endpoint other than
    # "default" — the accessor must read that endpoint's extra, not the
    # default alias's.
    cfg = ModelsConfig()
    cfg.llm["default"] = ModelEndpointConfig(endpoint="http://vllm:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: 8192})
    cfg.llm["mistral"] = ModelEndpointConfig(
        endpoint="http://mistral:8000/v1", extra={LLM_CONTEXT_SIZE_KEY: 32768, LLM_OUTPUT_TOKENS_KEY: 4096}
    )
    assert cfg.llm_context_size("mistral") == 32768
    assert cfg.llm_output_tokens("mistral") == 4096
    assert cfg.llm_context_size("default") == 8192
    assert cfg.llm_context_size("unregistered") is None
