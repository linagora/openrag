"""Prompt repository interface.

Backs the DB prompt library: a global set of named prompt templates with at
most one ``is_default`` per type. Selection (which prompt a preset/partition
uses) is by name, resolved in ``PromptService`` (named prompt → global default
→ disk seed); the repository only exposes the storage primitives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.prompt import Prompt


class PromptRepository(ABC):
    """CRUD + default-selection + per-partition override storage for prompts."""

    # ------------------------------------------------------------------
    # Library CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    async def create(self, prompt: Prompt) -> Prompt: ...

    @abstractmethod
    async def get(self, prompt_id: str) -> Prompt | None: ...

    @abstractmethod
    async def list(
        self,
        *,
        prompt_type: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Prompt]: ...

    @abstractmethod
    async def count(self, *, prompt_type: str | None = None) -> int: ...

    @abstractmethod
    async def update(self, prompt_id: str, **fields: object) -> Prompt | None:
        """Update whitelisted columns (``name``, ``content``). ``is_default`` is
        deliberately not updatable here — flip it through :meth:`set_default`,
        which clears the previous default in the same transaction (the partial
        unique index forbids two defaults per type)."""
        ...

    @abstractmethod
    async def delete(self, prompt_id: str) -> bool: ...

    # ------------------------------------------------------------------
    # Global default (one per type)
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_by_name(self, prompt_type: str, name: str) -> Prompt | None:
        """Look up a library prompt by (type, name) — the selection primitive.

        Presets and partitions select a prompt by naming it; this resolves that
        name to the stored prompt (``None`` if no such name exists for the type)."""
        ...

    # ------------------------------------------------------------------
    # Global default (one per type)
    # ------------------------------------------------------------------

    @abstractmethod
    async def reference_counts(self) -> dict[tuple[str, str], int]:
        """``{(prompt_type, name): partitions_referencing}`` in one bulk pass.

        Counts the *partitions* that reference each prompt by name — directly for
        generation prompts (``generation_prompt_names``), transitively for
        indexation/retrieval prompts (partition -> its active preset ->
        ``*_prompt_name`` config field). Feeds the admin "used by N partitions"
        annotation."""
        ...

    @abstractmethod
    async def get_default(self, prompt_type: str) -> Prompt | None: ...

    @abstractmethod
    async def set_default(self, prompt_id: str) -> Prompt | None:
        """Promote ``prompt_id`` to the default for its type, atomically.

        Clears any existing default of the same type and sets this one inside a
        single locked transaction. Returns the promoted row, or ``None`` if
        ``prompt_id`` does not exist."""
        ...
