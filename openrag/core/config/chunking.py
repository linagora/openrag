"""Chunking configuration."""

from __future__ import annotations

from pydantic import Field

from .base import ConfigMixin


class ChunkerConfig(ConfigMixin):
    """Chunking strategy settings."""

    name: str = "recursive_splitter"
    contextual_retrieval: bool = True
    contextualization_timeout: int = 120
    max_concurrent_contextualization: int = 10
    chunk_size: int = Field(default=512, gt=0)
    # Bounded to [0, 1): an overlap >= chunk_size makes the recursive splitter
    # raise at construction ("larger chunk overlap than chunk size"), so a bad
    # CHUNK_OVERLAP_RATE must fail at config load, not per-file at index time.
    chunk_overlap_rate: float = Field(default=0.2, ge=0.0, lt=1.0)
