"""Prompt repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from openrag.core.models.prompt import Prompt


class PromptRepository(ABC):
    """CRUD operations for prompt templates."""

    @abstractmethod
    async def create_prompt(self, prompt: Prompt) -> Prompt: ...

    @abstractmethod
    async def get_prompt(self, prompt_id: str) -> Prompt | None: ...

    @abstractmethod
    async def get_by_type(self, prompt_type: str) -> list[Prompt]: ...

    @abstractmethod
    async def get_active(self, prompt_type: str) -> Prompt | None: ...

    @abstractmethod
    async def list_prompts(self) -> list[Prompt]: ...

    @abstractmethod
    async def update_prompt(self, prompt_id: str, content: str) -> Prompt | None: ...

    @abstractmethod
    async def delete_prompt(self, prompt_id: str) -> bool: ...
