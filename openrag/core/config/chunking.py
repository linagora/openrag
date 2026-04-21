"""Chunking configuration."""

from __future__ import annotations

from openrag.core.config.base import ConfigMixin


class ChunkerConfig(ConfigMixin):
    """Chunking strategy settings."""

    name: str = "recursive_splitter"
    contextual_retrieval: bool = True
    contextualization_timeout: int = 120
    max_concurrent_contextualization: int = 10
    chunk_size: int = 512
    chunk_overlap_rate: float = 0.2
