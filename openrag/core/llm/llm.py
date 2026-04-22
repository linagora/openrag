"""Abstract LLM interface."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from openrag.core.utils.exceptions import LLMParsingError


class LLM(ABC):
    """Base class for all LLM providers."""

    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> str:
        """Generate a completion for a prompt."""
        ...

    @abstractmethod
    async def chat(self, messages: list[dict[str, str]], **kwargs) -> str:
        """Chat completion with message list."""
        ...

    async def generate_json(self, prompt: str, **kwargs) -> dict:
        """Generate a JSON response. Default: parse generate() output.

        Raises LLMParsingError if the LLM output is not valid JSON
        or if the result is not a dict.
        """
        response = await self.generate(prompt, **kwargs)
        text = response.strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMParsingError(
                raw_response=text,
                parse_error=str(exc),
            ) from exc
        if not isinstance(result, dict):
            raise LLMParsingError(
                raw_response=text,
                parse_error=f"Expected JSON object, got {type(result).__name__}",
            )
        return result

    async def chat_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        tool_choice: str | dict = "required",
        **kwargs,
    ) -> dict:
        """Chat completion with function calling.

        Only supported by backends with function calling (e.g., vLLM).
        Default raises NotImplementedError.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support tool calling")

    async def stream_chat(self, messages: list[dict[str, str]], **kwargs) -> AsyncIterator[str]:
        """Stream chat completion. Default falls back to non-streaming."""
        result = await self.chat(messages, **kwargs)
        yield result
