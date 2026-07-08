"""Abstract embedder interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    """Base class for all embedding providers."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning vectors."""
        ...

    @abstractmethod
    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return the embedding dimension."""
        ...
