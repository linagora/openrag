"""Chunking configuration."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

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

    # ``structured_section`` knobs (ignored by ``recursive_splitter``). Sizing
    # defaults derive from ``chunk_size`` when left None (target=chunk_size,
    # max≈1.5×, min≈¼×). ``heading_keywords`` / ``leaf_patterns`` make boundary
    # detection generalize beyond French legal codes; None uses the built-ins.
    # Constrained > 0 for the same reason as ``chunk_size``: a bad value must
    # fail at config load, not per-file at index time. A non-positive
    # ``hard_max_tokens`` would collapse the split budget to one token per piece
    # and shred atomic units; a non-positive ``max_tokens`` silently degrades to
    # ``min_tokens``.
    min_tokens: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    # Safety bound for atomic units (figure captions), distinct from
    # ``max_tokens``: it exists only to keep a pathological unit from
    # overflowing the embedder's context window, so leaving it ``None`` lets
    # ``create_chunker`` derive it from the embedder actually used by the
    # partition (half its window). Set it explicitly only to override that.
    hard_max_tokens: int | None = Field(default=None, gt=0)
    prepend_heading_path: bool = True
    # Layout of the source document, deciding whether a *page* is a meaningful
    # chunk boundary. "auto" detects it from the parsed pages (see
    # StructuredSectionChunker._looks_paginated); "paginated" and "flowing"
    # force it. Deliberately conservative in auto: chunking a 400-page report
    # per page would be far worse than not firing on a real deck.
    layout: Literal["auto", "paginated", "flowing"] = "flowing"
    heading_keywords: list[str] | None = None
    leaf_patterns: list[str] | None = None

    @field_validator("leaf_patterns")
    @classmethod
    def _validate_leaf_patterns(cls, patterns: list[str] | None) -> list[str] | None:
        """Compile every pattern at config load.

        ``StructuredSectionChunker.__init__`` compiles these, so an invalid
        regex would otherwise surface as an ``re.error`` per file at index time
        — long after the operator saved the preset, and on the indexing path
        rather than the configuration boundary.
        """
        for pattern in patterns or ():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid leaf_patterns regex {pattern!r}: {exc}") from exc
        return patterns
