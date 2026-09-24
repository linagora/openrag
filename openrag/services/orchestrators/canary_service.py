"""Synthetic canary: index a known document, retrieve it, delete it.

Every other signal measures a component from the inside, and all of them can be
green while the product is broken: an embedder swapped for one of another
dimension, a collection that is not loaded, retrieval quietly returning
nothing. The canary walks the path a user's document takes, through the same
services the upload, chat and delete routes call, and reports whether it came
out the other end.

What it deliberately does not do:

* **Touch anyone's data.** It works in its own reserved partition as its own
  system user, which holds no token and is not an admin. It deletes only files
  it created, whether the run passed or not, and a later run removes what a
  crashed one left behind.
* **Spend a person's quota.** Its files are counted against the canary user,
  which has no quota and is nobody's account.
* **Distort request metrics.** It calls the services, not the HTTP API, so none
  of its traffic reaches the per-route request counters. Metrics recorded
  below the API that must leave it out can test ``is_canary_partition``.

It retrieves through the chat pipeline (``RetrievalService.retrieve``) rather
than the raw search route, so the partition's configured retriever and
reranker are exercised too. Answer generation is not: the model endpoints'
readiness already covers the LLM, and a generated answer cannot be asserted on.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config.model_endpoints import DEFAULT_ENDPOINT_ALIAS
from core.models.catalog import DocumentStatus
from core.models.query import Query
from core.models.user import User
from core.observability.canary import (
    CANARY_FILE_ID_PREFIX,
    CANARY_PARTITION,
    CANARY_USER_DISPLAY_NAME,
    CANARY_USER_EMAIL,
    CanaryRunResult,
    CanaryStage,
)
from core.observability.monitoring import CANARY_METRICS, CanaryMetrics
from core.utils.exceptions import ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.canary import CanaryConfig
    from core.config.root import Settings
    from core.ports.document_repo import DocumentRepository
    from core.ports.job_repo import JobRepository
    from core.ports.user_repo import UserRepository
    from core.vector_stores.vector_store import VectorStore
    from services.orchestrators.indexing_service import IndexingService
    from services.orchestrators.partition_service import PartitionService
    from services.orchestrators.retrieval_service import RetrievalService
    from services.persistence.advisory_lease import AdvisoryLease

logger = get_logger()

# Lease name: one canary per deployment, however many API replicas.
CANARY_LEASE_KEY = "openrag:canary"

_PARTITION_DESCRIPTION = "Synthetic canary, managed by OpenRag. Its documents are created and deleted automatically."
_POLL_INTERVAL_SECONDS = 1.0
# Reasons go to logs only, never to metric labels, but a worker traceback can
# run to thousands of characters.
_MAX_REASON_CHARS = 1000


@dataclass(frozen=True)
class CanaryDocument:
    """One run's document: unique, so a hit can only be this run's."""

    file_id: str
    phrase: str

    @classmethod
    def create(cls, now: float, nonce: str) -> CanaryDocument:
        return cls(file_id=f"{CANARY_FILE_ID_PREFIX}{int(now)}-{nonce}", phrase=f"canary {nonce}")

    @property
    def filename(self) -> str:
        return f"{self.file_id}.txt"

    @property
    def text(self) -> str:
        return (
            "OpenRag synthetic canary document.\n\n"
            f"Verification phrase: {self.phrase}.\n\n"
            "This document is indexed, retrieved and deleted automatically to check that search works end to end.\n"
        )

    @property
    def query(self) -> str:
        return f"Verification phrase: {self.phrase}"


def canary_file_created_at(file_id: str) -> int | None:
    """The creation time encoded in a canary file id, or ``None`` if it is not one."""
    if not file_id.startswith(CANARY_FILE_ID_PREFIX):
        return None
    stamp, _, nonce = file_id[len(CANARY_FILE_ID_PREFIX) :].partition("-")
    if not stamp.isdigit() or not nonce:
        return None
    return int(stamp)


class _StageFailure(Exception):
    def __init__(self, stage: CanaryStage, reason: str) -> None:
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _error_summary(error: str | None) -> str:
    """The exception line of a task error, which the worker records as a whole traceback."""
    lines = [line.strip() for line in (error or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no error recorded"


def _bounded(reason: str) -> str:
    return reason if len(reason) <= _MAX_REASON_CHARS else f"{reason[:_MAX_REASON_CHARS]}…"


class CanaryService:
    """Run the canary once. Never raises for a failed check: it returns the outcome."""

    def __init__(
        self,
        *,
        indexing_service: IndexingService,
        retrieval_service: RetrievalService,
        partition_service: PartitionService,
        user_repo: UserRepository,
        document_repo: DocumentRepository,
        vector_store: VectorStore,
        settings: Settings,
        job_repo: JobRepository | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._indexing = indexing_service
        self._retrieval = retrieval_service
        self._partitions = partition_service
        self._users = user_repo
        self._documents = document_repo
        self._vectors = vector_store
        self._jobs = job_repo
        self._settings = settings
        self._config: CanaryConfig = settings.canary
        self._collection = settings.vectordb.collection_name
        # A subdirectory of the upload directory: the indexing workers read the
        # canary document from the same storage they read uploads from.
        self._document_dir = Path(settings.paths.data_dir) / "canary"
        self._poll_interval = poll_interval
        self._monotonic = monotonic
        self._wall_clock = wall_clock

    @property
    def _stale_after_seconds(self) -> float:
        """Age past which a canary file cannot belong to a run still in progress."""
        return self._config.index_timeout_seconds + 3 * self._config.request_timeout_seconds

    async def run_once(self) -> CanaryRunResult:
        started = self._monotonic()
        durations: dict[CanaryStage, float] = {}
        document = CanaryDocument.create(self._wall_clock(), secrets.token_hex(8))
        failure: _StageFailure | None = None
        path: Path | None = None

        try:
            async with self._stage(CanaryStage.SETUP, durations):
                user = await self._ensure_user()
                await self._ensure_partition(user.id)
                await self._prepare_partition()
                path = await asyncio.to_thread(self._write_document, document)
            task_id = await self._index(document, path, user, durations)
            await self._await_indexed(task_id, durations)
            async with self._stage(CanaryStage.QUERY, durations):
                await self._assert_retrievable(document)
        except _StageFailure as exc:
            failure = exc

        # Runs whether or not the checks passed. Skipped only when the run is
        # cancelled (shutdown); the next run's setup sweeps what that left.
        if path is not None:
            cleanup_error = await self._cleanup(document, path, durations)
            if cleanup_error is not None:
                if failure is None:
                    failure = _StageFailure(CanaryStage.CLEANUP, cleanup_error)
                else:
                    # The earlier stage stays the reported one, but the reason
                    # must still say the document may be left behind.
                    failure = _StageFailure(failure.stage, f"{failure.reason}; cleanup also failed: {cleanup_error}")

        total = self._monotonic() - started
        if failure is None:
            return CanaryRunResult(passed=True, durations=durations, total_seconds=total)
        return CanaryRunResult(
            passed=False,
            failed_stage=failure.stage,
            reason=_bounded(failure.reason),
            durations=durations,
            total_seconds=total,
        )

    @asynccontextmanager
    async def _stage(self, stage: CanaryStage, durations: dict[CanaryStage, float]) -> AsyncIterator[None]:
        started = self._monotonic()
        try:
            yield
        except _StageFailure:
            raise
        except Exception as exc:
            raise _StageFailure(stage, _describe(exc)) from exc
        finally:
            durations[stage] = durations.get(stage, 0.0) + self._monotonic() - started

    async def _within(self, awaitable: Any) -> Any:
        return await asyncio.wait_for(awaitable, timeout=self._config.request_timeout_seconds)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _ensure_user(self) -> User:
        user = await self._users.get_user_by_email(CANARY_USER_EMAIL)
        if user is not None:
            return user
        try:
            user = await self._users.create_user(
                User(
                    display_name=CANARY_USER_DISPLAY_NAME,
                    email=CANARY_USER_EMAIL,
                    is_admin=False,
                    # Unlimited: the canary must never be refused for quota.
                    file_quota=-1,
                )
            )
        except Exception:
            # Another replica created it between the lookup and the insert.
            user = await self._users.get_user_by_email(CANARY_USER_EMAIL)
            if user is None:
                raise
            return user
        logger.bind(user_id=user.id).info("Created the synthetic canary user")
        return user

    async def _ensure_partition(self, user_id: int) -> None:
        """Make sure the canary partition exists and the canary user owns it.

        Refuses a partition someone else owns: the name is reserved, but a
        deployment may predate the reservation, and the canary must never
        index into or clean up a person's partition.
        """
        if not await self._partitions.partition_exists(CANARY_PARTITION):
            try:
                await self._partitions.create_partition(
                    CANARY_PARTITION,
                    user_id=user_id,
                    description=_PARTITION_DESCRIPTION,
                    system=True,
                )
                logger.bind(partition=CANARY_PARTITION).info("Created the synthetic canary partition")
                return
            except ValidationError as exc:
                if exc.code != "PARTITION_EXISTS":
                    raise
        roles = {member["user_id"]: member["role"] for member in await self._partitions.list_members(CANARY_PARTITION)}
        if roles.get(user_id) == "owner":
            return
        other_owners = sorted(uid for uid, role in roles.items() if role == "owner")
        if other_owners:
            raise _StageFailure(
                CanaryStage.SETUP,
                f"partition '{CANARY_PARTITION}' is owned by user(s) {other_owners}, not the canary user "
                f"{user_id}; rename or delete it so the canary can create its own",
            )
        # The canary user was deleted and recreated: the partition survived
        # its old owner's membership.
        if user_id in roles:
            await self._partitions.update_role(CANARY_PARTITION, user_id, "owner")
        else:
            await self._partitions.add_member(CANARY_PARTITION, user_id, "owner")

    async def _prepare_partition(self) -> None:
        """Remove what earlier runs left behind, then follow the default embedder.

        A file is removed only once it is too old to belong to a run still in
        progress, so two replicas briefly both believing they hold the lease
        cannot delete each other's document. Files that are not canary files
        are someone's upload through an admin bypass: left alone.
        """
        cutoff = self._wall_clock() - self._stale_after_seconds
        remaining = 0
        for file in await self._partitions.list_files(CANARY_PARTITION):
            file_id = str(file.get("file_id") or "")
            created_at = canary_file_created_at(file_id)
            if created_at is None or created_at > cutoff:
                remaining += 1
                continue
            logger.bind(partition=CANARY_PARTITION, file_id=file_id).warning(
                "Deleting a document an earlier canary run left behind"
            )
            await self._within(self._indexing.delete_file(file_id, CANARY_PARTITION))
        await asyncio.to_thread(self._remove_stale_documents, cutoff)

        # The first write pins a partition to the embedder the ``default``
        # alias names at that moment, and an emptied partition stays pinned.
        # Opting back into the alias before every run keeps the canary on the
        # embedder new documents get today, not the one it met first.
        cached = self._settings.partitions.get(CANARY_PARTITION)
        if remaining == 0 and cached is not None and cached.embedder != DEFAULT_ENDPOINT_ALIAS:
            await self._partitions.update_partition(CANARY_PARTITION, embedder=DEFAULT_ENDPOINT_ALIAS)

    def _remove_stale_documents(self, cutoff: float) -> None:
        if not self._document_dir.is_dir():
            return
        for stale in self._document_dir.glob(f"{CANARY_FILE_ID_PREFIX}*.txt"):
            with suppress(OSError):
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()

    def _write_document(self, document: CanaryDocument) -> Path:
        self._document_dir.mkdir(parents=True, exist_ok=True)
        path = self._document_dir / document.filename
        path.write_text(document.text, encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    async def _index(
        self,
        document: CanaryDocument,
        path: Path,
        user: User,
        durations: dict[CanaryStage, float],
    ) -> str:
        # Submission counts as queue time: until a worker picks the task up,
        # the document has not started indexing.
        async with self._stage(CanaryStage.QUEUE, durations):
            return await self._within(
                self._indexing.add_file(
                    file_path=str(path),
                    file_id=document.file_id,
                    partition=CANARY_PARTITION,
                    metadata={},
                    sanitized_filename=document.filename,
                    original_filename=document.filename,
                    user={"id": user.id},
                )
            )

    async def _await_indexed(self, task_id: str, durations: dict[CanaryStage, float]) -> None:
        """Poll the task until it settles, splitting the wait into queue and index time."""
        started = self._monotonic()
        deadline = started + self._config.index_timeout_seconds
        running_since: float | None = None
        state: str | None = None
        while True:
            try:
                state = await self._within(self._indexing.get_task_state(task_id))
            except Exception as exc:
                stage = CanaryStage.QUEUE if running_since is None else CanaryStage.INDEX
                raise _StageFailure(stage, f"reading the task state failed: {_describe(exc)}") from exc
            now = self._monotonic()
            if running_since is None and state not in (None, DocumentStatus.QUEUED.value):
                running_since = now
                durations[CanaryStage.QUEUE] = durations.get(CanaryStage.QUEUE, 0.0) + now - started
            if running_since is not None:
                durations[CanaryStage.INDEX] = now - running_since
            if state == DocumentStatus.COMPLETED.value:
                await self._split_by_job_record(task_id, durations)
                return
            if state in (DocumentStatus.FAILED.value, DocumentStatus.CANCELLED.value):
                error = None
                with suppress(Exception):
                    error = await self._within(self._indexing.get_task_error(task_id))
                raise _StageFailure(CanaryStage.INDEX, f"indexing task {task_id} {state}: {_error_summary(error)}")
            if now >= deadline:
                if running_since is None:
                    durations[CanaryStage.QUEUE] = durations.get(CanaryStage.QUEUE, 0.0) + now - started
                    raise _StageFailure(
                        CanaryStage.QUEUE,
                        f"indexing task {task_id} still {state or 'unknown'} after "
                        f"{self._config.index_timeout_seconds}s: no worker picked it up",
                    )
                raise _StageFailure(
                    CanaryStage.INDEX,
                    f"indexing task {task_id} still {state} after {self._config.index_timeout_seconds}s",
                )
            await asyncio.sleep(self._poll_interval)

    async def _split_by_job_record(self, task_id: str, durations: dict[CanaryStage, float]) -> None:
        """Take the index time from the job record rather than from polling.

        Polling notices a state change up to one poll interval late, so a
        document indexed in under a second shows its whole wait as queue time.
        The durable job row has the worker's own ``started_at`` and
        ``completed_at``: index time is the difference, and queue time is the
        rest of the wait, submission included. Best-effort: the row is written
        asynchronously and may not show completion yet, in which case the
        polled split stands.
        """
        if self._jobs is None:
            return
        try:
            job = await self._within(self._jobs.get_job(task_id))
        except Exception:
            return
        if job is None or job.started_at is None or job.completed_at is None:
            return
        waited = durations.get(CanaryStage.QUEUE, 0.0) + durations.get(CanaryStage.INDEX, 0.0)
        indexing = min(waited, max(0.0, (job.completed_at - job.started_at).total_seconds()))
        durations[CanaryStage.INDEX] = indexing
        durations[CanaryStage.QUEUE] = waited - indexing

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    async def _assert_retrievable(self, document: CanaryDocument) -> None:
        chunks = await self._within(
            self._retrieval.retrieve(partitions=[CANARY_PARTITION], query=Query(query=document.query))
        )
        if any(chunk.partition == CANARY_PARTITION and chunk.document_id == document.file_id for chunk in chunks):
            return
        returned = sorted({chunk.document_id for chunk in chunks})
        raise _StageFailure(
            CanaryStage.QUERY,
            f"retrieval returned {len(chunks)} chunk(s) from {returned or 'no document'}, none from {document.file_id}",
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup(self, document: CanaryDocument, path: Path, durations: dict[CanaryStage, float]) -> str | None:
        """Delete this run's document and prove it is gone. Returns why not, or ``None``.

        Deleting a document that never made it into the catalog is a no-op, so
        this runs whatever stage the run stopped at. The delete also cancels an
        indexing task still in flight, which is what stops a timed-out run from
        landing its document after the check gave up on it.
        """
        started = self._monotonic()
        try:
            await self._within(self._indexing.delete_file(document.file_id, CANARY_PARTITION))
            if await self._within(self._documents.file_exists_in_partition(document.file_id, CANARY_PARTITION)):
                return f"catalog row for {document.file_id} survived the delete"
            if await self._within(self._vectors.collection_exists(self._collection)):
                leftover = await self._within(
                    self._vectors.query_ids_by_filter(
                        self._collection, {"partition": CANARY_PARTITION, "file_id": document.file_id}
                    )
                )
                if leftover:
                    return f"{len(leftover)} chunk(s) of {document.file_id} survived the delete"
            return None
        except Exception as exc:
            return f"deleting {document.file_id} failed: {_describe(exc)}"
        finally:
            with suppress(OSError):
                await asyncio.to_thread(path.unlink, missing_ok=True)
            durations[CanaryStage.CLEANUP] = self._monotonic() - started


class CanaryScheduler:
    """Run the canary on a fixed cadence, on whichever replica holds the lease."""

    def __init__(
        self,
        *,
        service: CanaryService,
        lease: AdvisoryLease,
        config: CanaryConfig,
        metrics: CanaryMetrics = CANARY_METRICS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._service = service
        self._lease = lease
        self._config = config
        self._metrics = metrics
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever(), name="openrag-canary")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._lease.release()
        finally:
            self._metrics.set_leader(False)

    async def run_if_leader(self) -> CanaryRunResult | None:
        """Run once if this replica holds the lease; ``None`` when another one does."""
        try:
            leader = await self._lease.acquire()
        except Exception as exc:
            # No Postgres, no lease: every replica stands down, and the
            # canary's own staleness is what reports it.
            logger.warning("Canary lease unavailable; skipping this run", error=_describe(exc))
            leader = False
        self._metrics.set_leader(leader)
        if not leader:
            return None

        try:
            result = await self._service.run_once()
        except Exception as exc:
            # run_once turns every failed check into a result; reaching this is
            # a bug in the canary itself, which must still count as a failure.
            logger.exception("Canary run crashed")
            result = CanaryRunResult(passed=False, reason=_bounded(_describe(exc)))
        self._metrics.record(result, finished_at=self._wall_clock())
        self._log(result)
        return result

    async def _run_forever(self) -> None:
        await self._sleep(self._config.initial_delay_seconds)
        while True:
            started = self._monotonic()
            try:
                await self.run_if_leader()
            except Exception:
                logger.exception("Canary scheduling iteration failed")
            # Start-to-start cadence: a slow run does not push every later one back.
            elapsed = self._monotonic() - started
            await self._sleep(max(0.0, self._config.interval_seconds - elapsed))

    @staticmethod
    def _log(result: CanaryRunResult) -> None:
        log = logger.bind(
            partition=CANARY_PARTITION,
            total_seconds=round(result.total_seconds, 3),
            stage_seconds={stage.value: round(seconds, 3) for stage, seconds in result.durations.items()},
        )
        if result.passed:
            log.info("Canary run passed")
        else:
            log.bind(
                failed_stage=result.failed_stage.value if result.failed_stage else None,
                reason=result.reason,
            ).warning("Canary run failed")
