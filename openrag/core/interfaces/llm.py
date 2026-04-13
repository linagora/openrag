from abc import ABC, abstractmethod
from typing import Any, AsyncIterator


class LLMClient(ABC):
    """Abstract interface for LLM completion services.

    Implementations: LLM (components/llm.py)
    """

    @abstractmethod
    async def completions(self, request: dict) -> AsyncIterator[Any]:
        """Generate a text completion.

        Yields response data (single dict for non-streaming).
        """
        ...
        # Make this a valid async generator for type checkers
        yield  # pragma: no cover

    @abstractmethod
    async def chat_completion(self, request: dict) -> AsyncIterator[Any]:
        """Generate a chat completion.

        Yields SSE lines (str) for streaming, or a single response dict
        for non-streaming.
        """
        ...
        yield  # pragma: no cover
