"""Tests for PresetsConfig frozen-field + mutable-dict invariant."""

from __future__ import annotations

import pytest
from core.config.indexation_pipeline import IndexationPipelineConfig
from core.config.presets import PresetsConfig
from core.config.retrieval_pipeline import RetrievalPipelineConfig
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


def test_indexation_pipeline_rejects_unknown_parsing_strategy():
    """Indexation presets reject parser names outside the supported set."""
    with pytest.raises(ValidationError):
        IndexationPipelineConfig(parsing_strategy="unknown")


def test_indexation_pipeline_rejects_unknown_contextualization_mode():
    """Indexation presets reject contextualization modes outside the supported set."""
    with pytest.raises(ValidationError):
        IndexationPipelineConfig(contextualization_mode="verbose")


def test_retrieval_pipeline_rejects_unknown_type():
    """Retrieval presets reject unsupported retrieval modes."""
    with pytest.raises(ValidationError):
        RetrievalPipelineConfig(type="unknown")


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_retrieval_pipeline_rejects_out_of_range_similarity_threshold(threshold: float):
    """Retrieval presets keep similarity thresholds in the normalized range."""
    with pytest.raises(ValidationError):
        RetrievalPipelineConfig(similarity_threshold=threshold)
