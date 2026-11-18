"""Unit tests for ``services.chat.prompt_settings.PartitionPromptSettings``."""

from __future__ import annotations

from types import SimpleNamespace

from core.config.retrieval_pipeline import RetrievalPipelineConfig
from services.chat.prompt_settings import PartitionPromptSettings


def _settings(**partitions) -> PartitionPromptSettings:
    """Settings over these partition configs, with ``reranker.top_k`` = 5 and a history depth of 4."""
    return PartitionPromptSettings(
        SimpleNamespace(
            reranker=SimpleNamespace(top_k=5), rag=SimpleNamespace(chat_history_depth=4), partitions=partitions
        )
    )


def _partition(top_n: int | None = None, **generation_prompt_names) -> SimpleNamespace:
    return SimpleNamespace(
        retrieval=RetrievalPipelineConfig(top_n=top_n), generation_prompt_names=generation_prompt_names
    )


def test_top_n_follows_the_partition_preset():
    assert _settings(p=_partition(top_n=15)).top_n(["p"]) == 15


def test_unset_top_n_follows_reranker_top_k():
    assert _settings(p=_partition(top_n=None)).top_n(["p"]) == 5


def test_top_n_takes_the_largest_across_partitions():
    settings = _settings(small=_partition(top_n=15), unset=_partition(top_n=None))

    assert settings.top_n(["small", "unset"]) == 15
    assert settings.top_n(["all"]) == 15


def test_top_n_falls_back_to_reranker_top_k():
    settings = _settings(p=_partition(top_n=15))

    assert settings.top_n(None) == 5
    assert settings.top_n(["unknown"]) == 5


def test_only_a_single_owning_partition_names_the_generation_prompt():
    settings = _settings(p=_partition(sys_prompt="legal"), q=_partition())

    assert settings.generation_prompt_name("sys_prompt", ["p"]) == "legal"
    assert settings.generation_prompt_name("spoken_style_answer", ["p"]) is None
    assert settings.generation_prompt_name("sys_prompt", ["p", "q"]) is None
    assert settings.generation_prompt_name("sys_prompt", ["all"]) is None
    assert settings.generation_prompt_name("sys_prompt", ["unknown"]) is None
    assert settings.generation_prompt_name("sys_prompt", None) is None
