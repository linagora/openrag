"""Transitional port for the indexing dispatch operations.

``IndexingService`` (Phase 8D.1) owns the business logic around file
ingestion — format/quota/existence checks, metadata assembly, workspace
validation — but the heavy lifting (serialize → chunk → embed → insert)
still runs inside the ``Indexer`` Ray actor, and task bookkeeping lives
in the ``TaskStateManager`` Ray actor. Phase 9 removes that Ray
indirection.

Defining the operations the service needs on a dedicated port keeps
``IndexingService`` Ray-free (8H: no Ray import / remote call under
``services/orchestrators/``). A small shim in ``services/storage/``
adapts the two Ray actors to this interface during the shim period;
Phase 9 swaps it for a direct pipeline call and deletes the shim.

No Ray / pymilvus / LangChain types leak across this boundary — only
plain dicts and strings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class IndexingDispatcher(ABC):
    """Operations the indexing orchestrator needs from the worker layer."""

    @abstractmethod
    async def dispatch_indexing(
        self,
        *,
        path: str,
        metadata: dict,
        partition: str,
        user: dict | None,
        workspace_ids: list[str] | None,
        replace: bool,
        indexation_config: dict | None = None,
        embedder_name: str | None = None,
    ) -> str:
        """Queue an (re)indexing job, register its task state, return its id."""
        ...

    @abstractmethod
    async def delete_file(self, file_id: str, partition: str) -> None:
        """Delete a file's chunks via the worker layer."""
        ...

    @abstractmethod
    async def update_file_metadata(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
    ) -> None:
        """Upsert file metadata in place (no re-embedding)."""
        ...

    @abstractmethod
    async def copy_file(
        self,
        file_id: str,
        metadata: dict,
        partition: str,
        user: dict | None,
    ) -> None:
        """Copy a file's chunks into another partition / file id."""
        ...

    @abstractmethod
    async def get_task_state(self, task_id: str) -> str | None:
        """Current task state, or ``None`` if the task is unknown."""
        ...

    @abstractmethod
    async def get_task_error(self, task_id: str) -> str | None:
        """Stored traceback for a failed task, or ``None``."""
        ...

    @abstractmethod
    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running/queued task.

        Returns ``False`` when the task cannot be cancelled, ``True`` once
        the cancel signal has been sent.
        """
        ...
