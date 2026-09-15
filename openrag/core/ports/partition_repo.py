"""Partition repository interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager


class PartitionRepository(ABC):
    """CRUD operations for partitions."""

    @abstractmethod
    async def create_partition(
        self, name: str, user_id: int | None = None, *, max_owned: int | None = None
    ) -> dict: ...

    @abstractmethod
    async def get_partition(self, name: str) -> dict | None: ...

    @abstractmethod
    async def list_partitions(self) -> list[dict]: ...

    @abstractmethod
    async def delete_partition(self, name: str) -> bool: ...

    @abstractmethod
    async def partition_exists(self, name: str) -> bool: ...

    @abstractmethod
    async def get_partition_row(self, name: str) -> dict | None: ...

    @abstractmethod
    async def list_partition_rows(self) -> list[dict]: ...

    @abstractmethod
    async def get_partition_file_count(self, partition: str) -> int: ...

    @abstractmethod
    async def count_files_by_partition(self) -> dict[str, int]: ...

    @abstractmethod
    async def update_partition(self, name: str, **fields: object) -> dict | None: ...

    @abstractmethod
    async def pin_default_embedder(self, name: str) -> str | None:
        """Replace a partition's ``default`` embedder alias with the endpoint it resolves to.

        Returns the partition's embedder afterwards, or ``None`` if it does not exist.
        """
        ...

    # ── Embedder swaps (#762 F4) ──────────────────────────────────────

    @abstractmethod
    async def start_embedder_swap(
        self, partition: str, *, source_embedder: str, target_embedder: str, files_total: int
    ) -> dict | None:
        """Record a running swap of *partition* onto *target_embedder*.

        Replaces the outcome of a previous swap. Returns ``None`` when a swap
        is already running. Must be atomic with a check that the target
        endpoint exists, so a concurrent delete or rename of it cannot slip in
        between — and once recorded, that delete sees the swap and is refused.

        Raises:
            NotFoundError: *target_embedder* names no embedder endpoint.
        """
        ...

    @abstractmethod
    async def get_embedder_swap(self, partition: str) -> dict | None:
        """The running swap of *partition*, or how its last one ended."""
        ...

    @abstractmethod
    async def list_embedder_swaps(self, status: str | None = None) -> list[dict]: ...

    @abstractmethod
    async def update_embedder_swap(self, partition: str, **fields: object) -> dict | None:
        """Update a *running* swap; ``None`` when it is no longer running.

        Conditional on the status, so a runner's progress write cannot revive
        a swap that was cancelled under it.
        """
        ...

    @abstractmethod
    async def complete_embedder_swap(self, partition: str) -> dict | None:
        """Point *partition* at its running swap's target and mark the swap completed.

        One transaction, so a concurrent cancel either lands first — and the
        partition keeps its embedder — or finds the swap no longer running.
        Returns ``None`` when the swap was not running.
        """
        ...

    @abstractmethod
    def embedder_swap_runner_lock(self, partition: str) -> AbstractAsyncContextManager[bool]:
        """Claim the right to run *partition*'s swap, across processes.

        Yields whether it was claimed, without waiting: ``False`` means another
        process is already running it. Released when the context exits or the
        holder dies.
        """
        ...
