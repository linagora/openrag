"""Embedder swap — moving a partition to another embedder in place (#762 F4)."""

from __future__ import annotations

from enum import StrEnum


class EmbedderSwapStatus(StrEnum):
    """Lifecycle of a partition's embedder swap.

    Only ``RUNNING`` locks the partition. The others are kept as the last
    outcome, so the UI can say how the previous swap ended.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


__all__ = ["EmbedderSwapStatus"]
