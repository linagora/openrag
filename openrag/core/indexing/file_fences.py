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
    COPY_CONTENT_CLAIM_TOKEN_PREFIX,
    FILE_FENCE_RENEW_INTERVAL_SECONDS,
    FILE_FENCE_TTL_SECONDS,
    FileFence,
    FileFenceKind,
)
from core.ports.file_fence_repo import FileFenceRepository
from core.utils.exceptions import ConflictError, FileFenceLostError, FileFenceUnavailableError
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


def target_exists(file_id: str, partition: str) -> ConflictError:
    """A copy into a ``file_id`` already taken: not temporary, unlike the fence refusals."""
    return ConflictError(
        f"File '{file_id}' already exists in partition '{partition}'.",
        code="DOCUMENT_ALREADY_EXISTS",
    )


def copy_in_progress(file_id: str, partition: str) -> ConflictError:
    """Same code as indexing: a copy writes the file the way an upload does, but has no task id."""
    return ConflictError(
        f"File '{file_id}' is being copied into partition '{partition}'. Please retry.",
        code="DOCUMENT_INDEXING_IN_PROGRESS",
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


def _retrieve_outcome(task: asyncio.Future[Any]) -> None:
    """Read the outcome of a renewal nobody awaits any more, so asyncio does not log it as lost."""
    if not task.cancelled():
        task.exception()


def busy(fence: FileFence) -> ConflictError:
    """The 409 for a file another operation holds."""
    if fence.kind is FileFenceKind.INDEX:
        if fence.holder_id.startswith(COPY_CONTENT_CLAIM_TOKEN_PREFIX):
            # A copy into the file: no task a client could poll.
            return copy_in_progress(fence.file_id, fence.partition)
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

    async def acquire_for_delete(
        self, *, partition: str, file_id: str, holder_id: str, take_over_indexing: bool = True
    ) -> None:
        """Take the fence for a delete: over indexing, after a delete or a metadata update.

        With ``take_over_indexing=False`` a file being indexed is refused with
        409 instead: for a delete that may turn out to have nothing to do. That
        includes a task whose job already reads as finished while its lease
        runs: its worker may still be writing (a job failed at startup by
        ``fail_orphaned_jobs``), and a delete with nothing to do must not cost
        it its fence.
        """
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
                take_over_finished_task=take_over_indexing,
            )
            if fence.holder_id == holder_id:
                return
            if fence.kind is FileFenceKind.INDEX and not take_over_indexing:
                raise busy(fence)
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
        """Take the fence for a metadata update, or refuse with 409.

        An indexing fence whose lease runs is refused even when its job already
        reads as finished: its worker may still be writing chunks (a job failed
        at startup by ``fail_orphaned_jobs``), and the update rewrites every
        chunk it reads, which could put back chunks that worker then removes.
        """
        if self._repo is None:
            return
        fence = await self._repo.acquire(
            partition=partition,
            file_id=file_id,
            kind=FileFenceKind.METADATA,
            holder_id=holder_id,
            ttl_seconds=FILE_FENCE_TTL_SECONDS,
            take_over_finished_task=False,
        )
        if fence.holder_id != holder_id:
            raise busy(fence)

    async def check_free(self, *, partition: str, file_id: str, own_holder_id: str | None = None) -> None:
        """Refuse with 409 if an operation other than *own_holder_id* holds the file."""
        if self._repo is None:
            return
        # Only a fence whose lease runs counts: a row left by a release that
        # failed stops refusing once its lease ends.
        fence = await self._repo.get_live(partition=partition, file_id=file_id)
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

        Call it right after taking the fence: the lease is counted from here. A
        database error during a renewal is retried at the next round, until less
        than one round of the confirmed lease is left; the work then stops with
        ``FileFenceUnavailableError`` (see ``_renew_before_expiry``).
        """
        if self._repo is None:
            return await work()
        work_task = asyncio.ensure_future(work())
        renewal = asyncio.ensure_future(self._keep_alive(partition=partition, file_id=file_id, holder_id=holder_id))
        try:
            done, _ = await asyncio.wait({work_task, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if work_task not in done:
                # The renewal only ends by raising: the fence was lost or could not be kept.
                await renewal
            # Work that finished is reported as it ended, even if the fence went
            # in the same round: its catalog write was checked against the fence.
            return await work_task
        finally:
            for task in (work_task, renewal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work_task, renewal, return_exceptions=True)

    async def hold(self, *, partition: str, file_id: str, holder_id: str, work: Callable[[], Awaitable[T]]) -> T:
        """``run_holding``, then release the fence, unless the work was stopped before it ended.

        Work stopped from outside (its lease could not be renewed, or its caller
        was cancelled) may have a vector-store write in flight that cancelling
        does not withdraw. Its fence is then left to expire rather than
        released: releasing would let another operation take the file at once,
        and the write could land after that operation started. Work that ended
        on its own, or whose fence another operation already took, releases as
        usual (the latter is a no-op).
        """
        stopped = False
        try:
            return await self.run_holding(partition=partition, file_id=file_id, holder_id=holder_id, work=work)
        except (FileFenceUnavailableError, asyncio.CancelledError):
            stopped = True
            raise
        finally:
            if stopped:
                logger.bind(partition=partition, file_id=file_id).warning(
                    "File work stopped before it ended; its fence is left to expire"
                )
            else:
                await self.release(partition=partition, file_id=file_id, holder_id=holder_id)

    async def _keep_alive(self, *, partition: str, file_id: str, holder_id: str) -> None:
        confirmed_until = monotonic() + FILE_FENCE_TTL_SECONDS
        while True:
            await asyncio.sleep(self._next_renewal_in(confirmed_until))
            confirmed_until = await self._renew_before_expiry(
                partition=partition, file_id=file_id, holder_id=holder_id, confirmed_until=confirmed_until
            )

    @staticmethod
    def _next_renewal_in(confirmed_until: float) -> float:
        """Seconds to wait before the next renewal: one round, or less when the deadline comes first."""
        deadline = confirmed_until - FILE_FENCE_RENEW_INTERVAL_SECONDS
        return max(min(FILE_FENCE_RENEW_INTERVAL_SECONDS, deadline - monotonic()), 0)

    async def _renew_before_expiry(
        self, *, partition: str, file_id: str, holder_id: str, confirmed_until: float
    ) -> float:
        """Renew the fence of work that must stop before it can be taken; return the new confirmed expiry.

        *confirmed_until* is the ``monotonic()`` time the lease is known to last
        until. A database error is retried at the next round until one round
        before that time; there this raises ``FileFenceUnavailableError`` (a
        503: nothing else touched the file). With the defaults that is after
        three failed rounds, 30 s before another operation could take the
        expired fence: the time a write the work had already sent has to
        complete. A fence another operation took raises ``FileFenceLostError``.
        """
        attempted_at = monotonic()
        deadline = confirmed_until - FILE_FENCE_RENEW_INTERVAL_SECONDS
        log = logger.bind(partition=partition, file_id=file_id)
        renewal = asyncio.ensure_future(self.renew(partition=partition, file_id=file_id, holder_id=holder_id))
        try:
            # Not wait_for: at its timeout it waits for the cancelled call to
            # unwind, and a cancellation that stalls would carry the work past
            # the deadline. A renewal still running at the deadline is left to
            # finish its cancellation on its own.
            done, _ = await asyncio.wait({renewal}, timeout=max(deadline - attempted_at, 0))
        finally:
            if not renewal.done():
                renewal.cancel()
                renewal.add_done_callback(_retrieve_outcome)
        if renewal not in done:
            log.warning("The file fence renewal did not answer before its lease ends; stopping")
            raise FileFenceUnavailableError(file_id, partition)
        try:
            held = renewal.result()
        except Exception as exc:  # noqa: BLE001 - retried at the next round, up to the deadline
            if monotonic() >= deadline:
                log.warning("Failed to renew the file fence before its lease ends; stopping", error=str(exc))
                raise FileFenceUnavailableError(file_id, partition) from exc
            log.warning("Failed to renew the file fence; retrying", error=str(exc))
            return confirmed_until
        if not held:
            raise FileFenceLostError(file_id, partition)
        return attempted_at + FILE_FENCE_TTL_SECONDS

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
    "copy_in_progress",
    "delete_in_progress",
    "indexing_in_progress",
    "metadata_update_in_progress",
    "target_exists",
]
