"""Tests for PresetsConfig frozen-field + mutable-dict invariant."""

from __future__ import annotations

import pytest
from core.config.presets import PresetsConfig
from pydantic import ValidationError


def test_presets_config_dicts_are_mutable_in_place():
    """Services perform clear()+update() atomic swaps on frozen PresetsConfig fields."""
    cfg = PresetsConfig()
    payload = {"chunking": {"chunk_size": 512}, "parsing_strategy": "marker"}

    cfg.indexation["default"] = payload
    assert "default" in cfg.indexation

    cfg.indexation.clear()
    cfg.indexation.update({"legal": payload})
    assert list(cfg.indexation) == ["legal"]


def test_presets_config_field_reassignment_raises():
    """Frozen ConfigMixin prevents field reassignment — only in-place mutation works."""
    cfg = PresetsConfig()
    with pytest.raises((TypeError, ValidationError)):
        cfg.indexation = {}  # type: ignore[misc]
