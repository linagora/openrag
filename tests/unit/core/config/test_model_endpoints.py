"""Tests for ModelsConfig frozen-field + mutable-dict invariant."""

from __future__ import annotations

import pytest
from core.config.model_endpoints import ModelEndpointConfig, ModelsConfig
from pydantic import ValidationError


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
