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


def test_indexation_pipeline_topic_tagging_defaults_off():
    """Topic tagging is opt-in: tags are not yet surfaced or used in retrieval."""
    assert IndexationPipelineConfig().enable_topic_tagging is False


def test_indexation_pipeline_accepts_preset_scoped_stt_and_asr_prompt():
    cfg = IndexationPipelineConfig(
        stt="moss-transcribe-diarize",
        asr_transcription_prompt_name="meeting-diarization",
    )

    assert cfg.stt == "moss-transcribe-diarize"
    assert cfg.asr_transcription_prompt_name == "meeting-diarization"


def test_retrieval_pipeline_rejects_unknown_type():
    """Retrieval presets reject unsupported retrieval modes."""
    with pytest.raises(ValidationError):
        RetrievalPipelineConfig(type="unknown")


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_retrieval_pipeline_rejects_out_of_range_similarity_threshold(threshold: float):
    """Retrieval presets keep similarity thresholds in the normalized range."""
    with pytest.raises(ValidationError):
        RetrievalPipelineConfig(similarity_threshold=threshold)


@pytest.mark.parametrize("rate", [1.0, 1.5, -0.1])
def test_indexation_pipeline_rejects_out_of_range_chunk_overlap_rate(rate: float):
    """overlap must be in [0, 1): >= chunk_size makes the splitter raise at
    construction, so it has to fail at config load, not per-file (#709)."""
    with pytest.raises(ValidationError):
        IndexationPipelineConfig(chunking={"chunk_size": 512, "chunk_overlap_rate": rate})


@pytest.mark.parametrize("rate", [0.0, 0.2, 0.99])
def test_indexation_pipeline_accepts_in_range_chunk_overlap_rate(rate: float):
    cfg = IndexationPipelineConfig(chunking={"chunk_size": 512, "chunk_overlap_rate": rate})
    assert cfg.chunking.chunk_overlap_rate == rate


def test_default_chunker_is_structured_section():
    """The default chunking strategy is ``structured_section``.

    Pinned because nothing else fails when the default flips: the name is a free
    string, every registered strategy validates, and a silent revert would only
    surface as a change in retrieval quality long after the fact. It is asserted
    on three surfaces at once because they must not drift apart — the Pydantic
    default, the ``conf/config.yaml`` shipped value, and the ``default``
    indexation preset seed all have to name the same strategy.
    """
    from pathlib import Path

    import core.chunking.factory  # noqa: F401  (registration side-effect)
    import yaml
    from core.chunking.registry import chunking_registry
    from core.config.chunking import ChunkerConfig
    from services.orchestrators.preset_service import _DEFAULT_SEEDS

    assert ChunkerConfig().name == "structured_section"
    assert IndexationPipelineConfig().chunking.name == "structured_section"
    assert _DEFAULT_SEEDS["indexation"]["default"]["chunking"]["name"] == "structured_section"

    shipped = yaml.safe_load(Path(__file__).parents[4].joinpath("conf/config.yaml").read_text())
    assert shipped["chunker"]["name"] == "structured_section"

    # A default that is not registered would fail every partition at chunker
    # build time (create_chunker) rather than at config load.
    assert ChunkerConfig().name in chunking_registry
