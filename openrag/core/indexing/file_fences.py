"""The rules every operation follows to take a file's fence (#1430).

One fence per ``(partition, file_id)`` (see ``FileFenceRepository``). Who wins
when two operations meet:

- indexing (an upload or a replace) is refused while anything else holds the
  file;
- a delete takes an indexing fence over: the indexing task can then no longer
  commit. It waits for another delete or a metadata update to end, up to
  ``wait_timeout``;
- a metadata update is refused while anything else holds the file;
- a copy takes an indexing fence on its target and holds nothing on its
  source: it checks that nothing changed the source while it read it
  (``check_unchanged``), so copies of one file can run side by side.

Every refusal is a 409 that says why the file is busy. Without a repository
(``FILE_FENCES=ray``) every method is a no-op.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar

from core.models.catalog import (
    FILE_FENCE_RENEW_INTERVAL_SECONDS,
    FILE_FENCE_TTL_SECONDS,
    FileFence,
    FileFenceKind,
)
from core.ports.file_fence_repo import FileFenceRepository
from core.utils.exceptions import ConflictError, FileFenceLostError
from core.utils.logging import get_logger

logger = get_logger()

_WAIT_INTERVAL_SECONDS = 0.5
# How long a delete waits for another delete or a metadata update to end.
DEFAULT_WAIT_TIMEOUT_SECONDS = 60.0

T = TypeVar("T")


def indexing_in_progress(file_id: str, partition: str, task_id: str) -> ConflictError:
    return ConflictError(
        f"File '{file_id}' is already being indexed in partition '{partition}'.",
        code="DOCUMENT_INDEXING_IN_PROGRESS",
        existing_task_id=task_id,
    )


def delete_in_progress(file_id: str, partition: str) -> ConflictError:
    return ConflictError(
        f"File '{file_id}' is being deleted from partition '{partition}'.",
        code="DOCUMENT_DELETE_IN_PROGRESS",
    )


def metadata_update_in_progress(file_id: str, partition: str) -> ConflictError:
    return ConflictError(
        f"The metadata of file '{file_id}' in partition '{partition}' is being updated. Please retry.",
        code="DOCUMENT_METADATA_UPDATE_IN_PROGRESS",
    )


def changed_during_copy(file_id: str, partition: str) -> ConflictError:
    return ConflictError(
        f"File '{file_id}' in partition '{partition}' changed while it was being copied. Please retry.",
        code="DOCUMENT_CHANGED_DURING_COPY",
    )


def busy(fence: FileFence) -> ConflictError:
    """The 409 for a file another operation holds."""
    if fence.kind is FileFenceKind.INDEX:
        return indexing_in_progress(fence.file_id, fence.partition, fence.holder_id)
    if fence.kind is FileFenceKind.DELETE:
        return delete_in_progress(fence.file_id, fence.partition)
    return metadata_update_in_progress(fence.file_id, fence.partition)


class FileFences:
    def __init__(self, repo: FileFenceRepository | None, *, wait_timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS) -> None:
        self._repo = repo
        self._wait_timeout = wait_timeout

    @property
    def enabled(self) -> bool:
        return self._repo is not None

    async def acquire_for_indexing(self, *, partition: str, file_id: str, holder_id: str) -> bool:
        """Take the fence for an upload or a replace, or refuse with 409.

        Returns whether a fence was taken: ``False`` without fences or without
        a file id.
        """
        if self._repo is None or not file_id:
            return False
        fence = await self._repo.acquire(
            partition=partition,
            file_id=file_id,
            kind=FileFenceKind.INDEX,
            holder_id=holder_id,
            ttl_seconds=FILE_FENCE_TTL_SECONDS,
        )
        if fence.holder_id != holder_id:
            raise busy(fence)
        return True

    async def acquire_for_delete(self, *, partition: str, file_id: str, holder_id: str) -> None:
        """Take the fence for a delete: over indexing, after a delete or a metadata update."""
        if self._repo is None:
            return
        deadline = monotonic() + self._wait_timeout
        while True:
            fence = await self._repo.acquire(
                partition=partition,
                file_id=file_id,
                kind=FileFenceKind.DELETE,
                holder_id=holder_id,
                ttl_seconds=FILE_FENCE_TTL_SECONDS,
            )
            if fence.holder_id == holder_id:
                return
            if fence.kind is FileFenceKind.INDEX:
                if await self._repo.take_over(
                    partition=partition,
                    file_id=file_id,
                    kind=FileFenceKind.DELETE,
                    holder_id=holder_id,
                    expected_holder_id=fence.holder_id,
                    ttl_seconds=FILE_FENCE_TTL_SECONDS,
                ):
                    return
                # The holder changed in between: look again.
                continue
            if monotonic() >= deadline:
                raise busy(fence)
            await asyncio.sleep(_WAIT_INTERVAL_SECONDS)

    async def acquire_for_metadata(self, *, partition: str, file_id: str, holder_id: str) -> None:
        """Take the fence for a metadata update, or refuse with 409."""
        if self._repo is None:
            return
        fence = await self._repo.acquire(
            partition=partition,
            file_id=file_id,
            kind=FileFenceKind.METADATA,
            holder_id=holder_id,
            ttl_seconds=FILE_FENCE_TTL_SECONDS,
        )
        if fence.holder_id != holder_id:
            raise busy(fence)

    async def check_free(self, *, partition: str, file_id: str, own_holder_id: str | None = None) -> None:
        """Refuse with 409 if an operation other than *own_holder_id* holds the file."""
        if self._repo is None:
            return
        fence = await self._repo.get(partition=partition, file_id=file_id)
        if fence is not None and fence.holder_id != own_holder_id:
            raise busy(fence)

    async def check_unchanged(
        self,
        *,
        partition: str,
        file_id: str,
        before: dict[str, Any],
        read_again: Callable[[], Awaitable[dict[str, Any] | None]],
        own_holder_id: str | None = None,
    ) -> None:
        """After reading a copy's source: refuse if anything held or changed it meanwhile.

        *before* is the source's catalog row read before its chunks were. An
        operation still running on it shows up as its fence; one that started
        and ended in between shows up as a different row (a replace writes a
        new ``indexed_at``, a metadata update new metadata).
        """
        if self._repo is None:
            return
        await self.check_free(partition=partition, file_id=file_id, own_holder_id=own_holder_id)
        if await read_again() != before:
            raise changed_during_copy(file_id, partition)

    async def run_holding(self, *, partition: str, file_id: str, holder_id: str, work: Callable[[], Awaitable[T]]) -> T:
        """Run *work* while renewing the fence; stop it with ``FileFenceLostError`` if the fence is lost.

        A database error during a renewal is retried at the next round; the
        lease outlasts three missed rounds.
        """
        if self._repo is None:
            return await work()
        work_task = asyncio.ensure_future(work())
        renewal = asyncio.ensure_future(self._keep_alive(partition=partition, file_id=file_id, holder_id=holder_id))
        try:
            done, _ = await asyncio.wait({work_task, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            return await work_task
        finally:
            for task in (work_task, renewal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work_task, renewal, return_exceptions=True)

    async def _keep_alive(self, *, partition: str, file_id: str, holder_id: str) -> None:
        while True:
            await asyncio.sleep(FILE_FENCE_RENEW_INTERVAL_SECONDS)
            try:
                held = await self.renew(partition=partition, file_id=file_id, holder_id=holder_id)
            except Exception as exc:  # noqa: BLE001 - retried at the next round
                logger.bind(partition=partition, file_id=file_id).warning(
                    "Failed to renew the file fence; retrying", error=str(exc)
                )
                continue
            if not held:
                raise FileFenceLostError(file_id, partition)

    async def renew(self, *, partition: str, file_id: str, holder_id: str) -> bool:
        """Extend the fence; ``False`` once ``holder_id`` no longer holds it, ``True`` without fences.

        A database error is raised: the caller decides whether to retry.
        """
        if self._repo is None:
            return True
        return await self._repo.renew(
            partition=partition, file_id=file_id, holder_id=holder_id, ttl_seconds=FILE_FENCE_TTL_SECONDS
        )

    async def release(self, *, partition: str, file_id: str, holder_id: str) -> None:
        """Drop the fence if ``holder_id`` holds it; a failure is logged, the lease expires on its own."""
        if self._repo is None:
            return
        try:
            await self._repo.release(partition=partition, file_id=file_id, holder_id=holder_id)
        except Exception as exc:  # noqa: BLE001 - the lease expires on its own
            logger.bind(partition=partition, file_id=file_id).warning(
                "Failed to release the file fence; it expires on its own",
                error=str(exc),
            )


__all__ = [
    "DEFAULT_WAIT_TIMEOUT_SECONDS",
    "FileFences",
    "busy",
    "changed_during_copy",
    "delete_in_progress",
    "indexing_in_progress",
    "metadata_update_in_progress",
]
