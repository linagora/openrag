"""File fence repository interface (#1430).

One fence per ``(partition, file_id)``, held by at most one operation at a
time: indexing (a new file or a replace, or a copy into it), a delete or a
metadata update. The fence lives in the catalog database, so it holds for
every process that shares it, whatever runs the operation.

The holder id is the fencing token. An indexing fence is held by its task id
and released by whichever process settles the task, so it is a row with an
expiry rather than a lock tied to one connection. The catalog write that makes
a file visible checks, in its own transaction, that it still holds the fence
(``DocumentRepository.add_file_to_partition`` and ``update_file_in_partition``
with ``fence_holder_id``), so an operation that lost its fence never commits.

Every time is the database's ``now()``, never the caller's clock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.models.catalog import FileFence, FileFenceKind


class FileFenceRepository(ABC):
    @abstractmethod
    async def acquire(
        self,
        *,
        partition: str,
        file_id: str,
        kind: FileFenceKind,
        holder_id: str,
        ttl_seconds: float,
    ) -> FileFence:
        """Take the fence if it is free, and return whoever holds it now.

        The caller holds the fence when the returned ``holder_id`` is its own.
        A fence is free when nobody holds it, when its lease has expired, or
        when it is an ``INDEX`` fence whose task's ``jobs`` row is in a
        terminal state. Acquiring again with the same ``holder_id`` renews it,
        so a retried admission is harmless. The partition row must exist.
        """

    @abstractmethod
    async def take_over(
        self,
        *,
        partition: str,
        file_id: str,
        kind: FileFenceKind,
        holder_id: str,
        expected_holder_id: str,
        ttl_seconds: float,
    ) -> bool:
        """Replace the holder, only if it is still ``expected_holder_id``.

        For a delete, which wins over indexing: the indexing task's catalog
        write then finds it no longer holds the fence and writes nothing.
        Waits for a catalog write that holds the fence to commit first.
        """

    @abstractmethod
    async def renew(self, *, partition: str, file_id: str, holder_id: str, ttl_seconds: float) -> bool:
        """Extend the lease. ``False`` when ``holder_id`` no longer holds the fence."""

    @abstractmethod
    async def renew_all(self, fences: list[tuple[str, str, str]], *, ttl_seconds: float) -> int:
        """Extend every ``(partition, file_id, holder_id)`` lease still held, in one statement.

        Returns how many were renewed. A fence its holder lost is skipped, and
        so is one a catalog commit has locked right now: the commit is the
        holder finishing, and waiting for it would hold up every other renewal.
        """

    @abstractmethod
    async def release(self, *, partition: str, file_id: str, holder_id: str) -> bool:
        """Drop the fence if ``holder_id`` holds it. A second release is a no-op."""

    @abstractmethod
    async def get(self, *, partition: str, file_id: str) -> FileFence | None:
        """The current fence, expired or not, or ``None``."""

    @abstractmethod
    async def get_live(self, *, partition: str, file_id: str) -> FileFence | None:
        """The fence whose lease still runs, or ``None``.

        The lease is what a running holder keeps renewing, so this is the
        fence an operation may still be writing under. Unlike ``acquire``, a
        terminal ``jobs`` row does not free an ``INDEX`` fence here: a job can
        turn terminal while its worker still writes. A row whose release
        failed stops counting once its lease ends.
        """
