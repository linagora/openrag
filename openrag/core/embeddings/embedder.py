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

    # Concrete rather than abstract: the shipped clients already store these
    # under these names, and one that doesn't reports ``None`` instead of
    # failing to instantiate. Override if yours keeps them elsewhere.

    @property
    def model_name(self) -> str | None:
        """Model this client asks the endpoint to run, if known."""
        return getattr(self, "_model", None)

    @property
    def endpoint(self) -> str | None:
        """Base URL this client embeds against, if known."""
        return getattr(self, "_endpoint", None)
