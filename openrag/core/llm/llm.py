"""Abstract LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class LLM(ABC):
    """Base class for all LLM providers."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> dict:
        """Generate a text completion for a prompt."""
        ...

    @abstractmethod
    async def chat(self, messages: list[dict[str, str]], **kwargs) -> dict:
        """Chat completion with message list."""
        ...

    @abstractmethod
    def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> AsyncIterator[str]:
        """Stream chat completion as raw SSE lines.

        Implementations must be ``async def`` generators yielding ``str`` chunks.
        Declared without ``async def`` here so the abstract signature matches the
        ``AsyncIterator[str]`` return type without forcing an empty ``yield``.
        """
        ...
