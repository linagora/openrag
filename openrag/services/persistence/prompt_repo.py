"""Stub :class:`PromptRepository`.

Prompts are currently disk-based templates (``components/prompts/``).
The post-refactoring P1 feature is DB-stored, per-partition,
versionable prompts that operators can edit without redeploying. When
that lands the on-disk templates become the seed for the table and
this stub becomes a real asyncpg implementation against a ``prompts``
table.
"""

from __future__ import annotations

from core.models.prompt import Prompt
from core.ports.prompt_repo import PromptRepository
from services.persistence._stubs import _StubRepositoryBase, stub_not_implemented


class PgPromptRepository(_StubRepositoryBase, PromptRepository):
    """TODO: real impl once the ``prompts`` table is added."""

    async def create_prompt(self, prompt: Prompt) -> Prompt:
        raise stub_not_implemented("DB-stored prompts")

    async def get_prompt(self, prompt_id: str) -> Prompt | None:
        raise stub_not_implemented("DB-stored prompts")

    async def get_by_type(self, prompt_type: str) -> list[Prompt]:
        raise stub_not_implemented("DB-stored prompts")

    async def get_active(self, prompt_type: str) -> Prompt | None:
        raise stub_not_implemented("DB-stored prompts")

    async def list_prompts(self) -> list[Prompt]:
        raise stub_not_implemented("DB-stored prompts")

    async def update_prompt(self, prompt_id: str, content: str) -> Prompt | None:
        raise stub_not_implemented("DB-stored prompts")

    async def delete_prompt(self, prompt_id: str) -> bool:
        raise stub_not_implemented("DB-stored prompts")


__all__ = ["PgPromptRepository"]
