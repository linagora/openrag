"""Tests for ModelsConfig frozen-field + mutable-dict invariant."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.config.model_endpoints import ModelEndpointConfig, ModelEndpointRow, ModelsConfig
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
