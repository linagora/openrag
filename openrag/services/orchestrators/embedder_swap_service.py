"""EmbedderSwapService — move a partition to another embedder in place (#762 F4).

Every embedder writes into its own dense field and a partition searches only
its embedder's field, so changing a partition's embedder without re-embedding
makes its files vanish from search. A swap re-embeds them first:

1. **Start.** Under the partition fence uploads are admitted under, refuse
   while indexing tasks are active, then record the swap. From then on, writes
   to the partition are refused until it ends.
2. **Re-embed, file by file.** Each file's stored chunk ``text`` — what the old
   embedder was given, contextualization included, so nothing is re-parsed,
   re-chunked or re-contextualized — goes through the target endpoint's
   :class:`Embedder`, and the vectors are written into the target field with a
   partial update. The partition keeps searching its current field throughout.
3. **Record.** The file's embedder record names the target model and field.
   That is also what makes a swap resumable: a file whose record already
   matches both is skipped, so a restart re-embeds nothing twice.
4. **Complete.** Under the fence, point the partition at the target embedder
   and mark the swap completed, in one transaction; uploads resume.

The old field's vectors for this partition are left where they are. Nothing
reads them once the partition has switched, a swap back re-embeds those files
again, and deleting the old embedder drops its whole field. Clearing them
after the partition unlocks would race a swap straight back to that embedder,
whose fresh vectors land in the same field.

A swap onto the embedder a partition already uses re-embeds only the files
recorded with another model or field — the repair for the drift #939 reports
after an endpoint was repointed.

The job runs as a task in the API process. Its state lives in Postgres, so a
restart resumes it (:meth:`resume_running`), and an advisory lock keeps one
runner per swap across processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.config.model_endpoints import DEFAULT_ENDPOINT_ALIAS
from core.models.embedder_swap import EmbedderSwapStatus
from core.utils.exceptions import ConflictError, PartitionNotFoundError, ValidationError, VDBError
from core.utils.logging import get_logger
from services.workers.pipeline_builder import embedder_provenance
from services.workers.task_cancellation import count_active_indexing_tasks

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.embeddings.embedder import Embedder
    from core.ports.document_repo import DocumentRepository
    from core.ports.partition_repo import PartitionRepository
    from core.vector_stores import VectorStore
    from services.orchestrators.partition_service import PartitionService

logger = get_logger()


class _SwapStopped(Exception):
    """The swap stopped running under the job: cancelled, or its partition deleted."""


class EmbedderSwapService:
    """Start, observe, cancel and run partition embedder swaps."""

    def __init__(
        self,
        *,
        partition_repo: PartitionRepository,
        document_repo: DocumentRepository,
        vector_store: VectorStore,
        partition_service: PartitionService,
        config: Settings,
        embedder_factory: Callable[[str], Embedder],
        collection: str,
        task_state_manager_factory: Callable[[], Any] | None = None,
        task_lookup_timeout: float = 60.0,
    ) -> None:
        self._partition_repo = partition_repo
        self._document_repo = document_repo
        self._vector_store = vector_store
        self._partition_service = partition_service
        self._config = config
        self._embedder_factory = embedder_factory
        self._collection = collection
        self._task_state_manager_factory = task_state_manager_factory
        self._task_lookup_timeout = task_lookup_timeout
        # Jobs running in this process, by partition.
        self._jobs: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    async def start(self, partition: str, target_embedder: str) -> dict:
        """Start re-embedding *partition* with *target_embedder*; return the swap."""
        if not await self._partition_service.partition_exists(partition):
            raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
        self._target_vector_field(target_embedder)

        async with self._partition_service.operation_lock(partition):
            active = await self._count_active_indexing_tasks(partition)
            if active:
                raise ConflictError(
                    f"Partition '{partition}' has {active} indexing task(s) in progress. "
                    "Start the embedder swap once they finish, so their files are re-embedded too.",
                    code="INDEXING_IN_PROGRESS",
                )
            row = await self._partition_repo.get_partition_row(partition)
            if row is None:
                raise PartitionNotFoundError(f"Partition '{partition}' does not exist.")
            files = await self._document_repo.list_file_embedders(partition)
            swap = await self._partition_repo.start_embedder_swap(
                partition,
                source_embedder=row.get("embedder") or DEFAULT_ENDPOINT_ALIAS,
                target_embedder=target_embedder,
                files_total=len(files),
            )
        if swap is None:
            raise ConflictError(
                f"An embedder swap is already running on partition '{partition}'.",
                code="EMBEDDER_SWAP_IN_PROGRESS",
            )

        logger.bind(partition=partition, target_embedder=target_embedder, files=len(files)).info(
            "Started embedder swap"
        )
        self._launch(partition)
        return _serialize(swap)

    async def get(self, partition: str) -> dict | None:
        """The running swap of *partition*, how its last one ended, or ``None``.

        ``None`` when the partition never swapped. That is the usual answer, not
        an error: the admin UI asks on every documents and partition page.

        Also restarts the job of a running swap no process is running — its
        runner died — so a swap is never stranded until the next restart.
        """
        swap = await self._partition_repo.get_embedder_swap(partition)
        if swap is None:
            return None
        if swap["status"] == EmbedderSwapStatus.RUNNING:
            self._launch(partition)
        return _serialize(swap)

    async def cancel(self, partition: str) -> dict:
        """Stop a running swap. The partition stays on its current embedder."""
        swap = await self._partition_repo.update_embedder_swap(
            partition,
            status=EmbedderSwapStatus.CANCELLED.value,
            finished_at=datetime.now(UTC),
        )
        if swap is None:
            raise ConflictError(
                f"No embedder swap is running on partition '{partition}'.",
                code="EMBEDDER_SWAP_NOT_RUNNING",
            )
        job = self._jobs.get(partition)
        if job is not None:
            job.cancel()
        logger.bind(partition=partition).info("Cancelled embedder swap")
        return _serialize(swap)

    async def resume_running(self) -> int:
        """Restart the job of every running swap. Called at startup."""
        swaps = await self._partition_repo.list_embedder_swaps(EmbedderSwapStatus.RUNNING.value)
        for swap in swaps:
            self._launch(swap["partition"])
        return len(swaps)

    async def shutdown(self) -> None:
        """Stop this process's jobs without changing their status, so they resume."""
        jobs = [job for job in self._jobs.values() if not job.done()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    # ------------------------------------------------------------------
    # Job
    # ------------------------------------------------------------------

    def _launch(self, partition: str) -> None:
        job = self._jobs.get(partition)
        if job is not None and not job.done():
            return
        self._jobs[partition] = asyncio.create_task(self._run(partition), name=f"embedder-swap:{partition}")

    async def _run(self, partition: str) -> None:
        async with self._partition_repo.embedder_swap_runner_lock(partition) as claimed:
            if not claimed:
                return  # another process runs it
            swap = await self._partition_repo.get_embedder_swap(partition)
            if swap is None or swap["status"] != EmbedderSwapStatus.RUNNING:
                return
            log = logger.bind(partition=partition, target_embedder=swap["target_embedder"])
            try:
                await self._reembed_partition(swap)
                if await self._complete(swap):
                    log.info("Completed embedder swap")
            except _SwapStopped:
                # Cancelled through the API, or the partition deleted.
                log.info("Embedder swap stopped")
            except asyncio.CancelledError:
                # Cancelled through the API in this process, or the process is
                # shutting down — then the swap stays running and resumes.
                log.info("Embedder swap job stopped")
                raise
            except Exception as exc:
                log.exception("Embedder swap failed")
                await self._partition_repo.update_embedder_swap(
                    partition,
                    status=EmbedderSwapStatus.FAILED.value,
                    error=str(exc) or type(exc).__name__,
                    finished_at=datetime.now(UTC),
                )

    async def _reembed_partition(self, swap: dict) -> None:
        partition = swap["partition"]
        target = swap["target_embedder"]
        field = self._target_vector_field(target)
        embedder = self._embedder_factory(target)
        model_name = getattr(embedder, "model_name", None)

        # Listed again rather than trusted from the start: files deleted since
        # need no work, and on a resume the count is what is left to check.
        files = await self._document_repo.list_file_embedders(partition)
        await self._progress(partition, files_total=len(files), files_done=0)
        for done, file in enumerate(files, start=1):
            already_there = (
                model_name is not None
                and file.get("embedder_model_name") == model_name
                and file.get("embedder_vector_field") == field
            )
            if not already_there:
                await self._reembed_file(partition, file["file_id"], target, embedder, field)
            # Doubles as the cancellation check: the update only applies to a
            # swap that is still running.
            await self._progress(partition, files_done=done)

    async def _reembed_file(self, partition: str, file_id: str, target: str, embedder: Embedder, field: str) -> None:
        rows = await self._vector_store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_id},
            output_fields=["_id", "text"],
        )
        if rows:
            vectors = await embedder.embed([row.get("text") or "" for row in rows])
            if len(vectors) != len(rows):
                raise ValueError(
                    f"Embedder '{target}' returned {len(vectors)} vectors for {len(rows)} chunks of '{file_id}'."
                )
            await self._vector_store.ensure_vector_field(field, len(vectors[0]))
            by_id = {str(row["_id"]): vector for row, vector in zip(rows, vectors, strict=True)}
            # Deleting a file stays allowed during a swap, and a write naming a
            # chunk deleted since is refused whole. Drop what is gone and write
            # the rest — repeatedly, because the next delete can land between
            # this query and the retry. The loop ends: each pass writes, or
            # strictly shrinks a finite set. A failure with nothing deleted is
            # a real one and propagates.
            while by_id:
                try:
                    await self._vector_store.write_vectors(field, by_id)
                    break
                except VDBError:
                    remaining = await self._vector_store.query_chunks_by_filter(
                        self._collection,
                        {"partition": partition, "file_id": file_id},
                        output_fields=["_id"],
                    )
                    still_there = {str(row["_id"]) for row in remaining}
                    if still_there >= by_id.keys():
                        raise
                    by_id = {chunk_id: vector for chunk_id, vector in by_id.items() if chunk_id in still_there}
        # Recorded even for a file with no chunks, so a resume skips it.
        await self._document_repo.record_file_embedder(file_id, partition, embedder_provenance(embedder, target, field))

    async def _complete(self, swap: dict) -> bool:
        partition = swap["partition"]
        # Under the fence uploads are admitted under, so no file is admitted
        # against the old embedder between the switch and the cache reload.
        async with self._partition_service.operation_lock(partition):
            completed = await self._partition_repo.complete_embedder_swap(partition)
            if completed is None:
                return False
            await self._partition_service.load_partitions()
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _target_vector_field(self, target_embedder: str) -> str:
        if target_embedder == DEFAULT_ENDPOINT_ALIAS:
            raise ValidationError(
                f"Name the embedder to swap to; '{DEFAULT_ENDPOINT_ALIAS}' follows whichever one is default.",
                code="EMBEDDER_ALIAS_NOT_ALLOWED",
            )
        endpoint = self._config.models.embedder.get(target_embedder)
        if endpoint is None:
            raise ValidationError(
                f"Embedder endpoint '{target_embedder}' not found.",
                code="MODEL_ENDPOINT_NOT_FOUND",
            )
        field = getattr(endpoint, "vector_field", None)
        if not field:
            raise ConflictError(
                f"Embedder '{target_embedder}' has no vector field; run the database migrations first.",
                code="EMBEDDER_VECTOR_FIELD_MISSING",
            )
        return field

    async def _progress(self, partition: str, **fields: int) -> None:
        if await self._partition_repo.update_embedder_swap(partition, **fields) is None:
            raise _SwapStopped

    async def _count_active_indexing_tasks(self, partition: str) -> int:
        if self._task_state_manager_factory is None:
            return 0
        return await count_active_indexing_tasks(
            self._task_state_manager_factory(),
            partition=partition,
            timeout=self._task_lookup_timeout,
        )


def _serialize(swap: dict) -> dict:
    """JSON-ready swap row."""
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in swap.items()}


__all__ = ["EmbedderSwapService"]
