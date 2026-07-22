"""Abstract LLM interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


def chat_content(response: Any) -> str:
    """Extract the assistant message text from an OpenAI-shaped chat response.

    ``LLM.chat`` returns the raw provider payload (a ``dict``), not a string —
    callers that want the text must reach into ``choices[0].message.content``.
    Doing that inline is easy to forget: ``multiQuery``/``hyde`` both treated the
    dict as a ``str`` and raised ``AttributeError`` on every query (#703).

    Returns ``""`` for a malformed or empty payload rather than raising, so a
    non-compliant provider degrades to "no expansion" instead of failing the
    whole search — callers already handle an empty result by falling back to the
    original query.
    """
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    return content if isinstance(content, str) else ""


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
