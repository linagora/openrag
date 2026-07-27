"""EvaluationService — datasets on disk, runs dispatched to the Ray actor.

Setup and teardown of a run's *identity* live here rather than in the worker,
because creating users and partitions is orchestration the API layer already
owns. The worker receives a partition it may write to and a token it may use,
and nothing else about the system.

The bearer token handed to the worker belongs to a single long-lived service
user (``__openrag_eval__``) whose token is **regenerated at the start of every
run**. That keeps exactly one non-admin service account in the database while
ensuring no usable plaintext token is ever stored at rest — the previous one
stops working the moment a new run starts.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.evaluation import parse_testset
from core.models.evaluation import (
    EVAL_PARTITION_PREFIX,
    EvalDataset,
    EvalRun,
    EvalRunStatus,
    is_eval_partition,
)
from core.models.user import UserCreate
from core.utils.exceptions import ConflictError, NotFoundError, OpenRAGError, ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import IO

    from core.config.root import Settings
    from core.ports.evaluation_repo import EvaluationRepository

logger = get_logger()

#: Stable identity of the service account runs authenticate as.
EVAL_USER_EXTERNAL_ID = "__openrag_eval__"
EVAL_USER_DISPLAY_NAME = "OpenRAG Evaluation"

TESTSET_FILENAME = "testset.csv"
CORPUS_DIRNAME = "corpus"

#: Block size for streaming an upload to disk.
_COPY_CHUNK_BYTES = 1024 * 1024

#: Bound on the pre-dispatch liveness check.
_RUNNER_PING_TIMEOUT_SECONDS = 60


def eval_partition_name(run_id: str) -> str:
    return f"{EVAL_PARTITION_PREFIX}{run_id}"


class EvaluationRunnerUnavailableError(OpenRAGError):
    """The runner actor could not be reached. Maps to HTTP 503."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="EVAL_RUNNER_UNAVAILABLE", status_code=503)


class EvaluationService:
    """Dataset storage plus run dispatch for the admin evaluation page."""

    def __init__(
        self,
        repo: EvaluationRepository,
        user_service: Any,
        user_repo: Any,
        partition_service: Any,
        config: Settings,
        *,
        runner_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._repo = repo
        self._user_service = user_service
        self._user_repo = user_repo
        self._partition_service = partition_service
        self._config = config
        self._settings = config.evaluation
        self._runner_factory = runner_factory
        self._root = Path(config.paths.data_dir) / "eval"

    # ── datasets ─────────────────────────────────────────────────────

    def _dataset_dir(self, dataset_id: str) -> Path:
        return self._root / dataset_id

    async def create_dataset(
        self,
        *,
        name: str,
        corpus: Sequence[tuple[str, IO[bytes]]],
        testset: IO[bytes],
        user_id: int | None,
    ) -> EvalDataset:
        """Validate and store a corpus + test set.

        The CSV is parsed here so a malformed test set is rejected at upload
        rather than after a run has already spent minutes indexing.

        Uploads arrive as open binary streams rather than ``bytes``: each is
        read under a size cap and copied to disk in fixed-size blocks, so a
        large corpus is never held in memory. The blocking file I/O runs on a
        worker thread so it cannot stall the event loop.
        """
        if not name.strip():
            raise ValidationError("Dataset name is required.", status_code=400)
        if not corpus:
            raise ValidationError("At least one corpus file is required.", status_code=400)

        testset_csv = await asyncio.to_thread(
            self._read_capped,
            testset,
            self._settings.max_testset_bytes,
            f"Test set exceeds the {self._settings.max_testset_mb} MB limit.",
        )
        cases = parse_testset(testset_csv, max_rows=self._settings.max_testset_rows)

        dataset_id = uuid.uuid4().hex
        directory = self._dataset_dir(dataset_id)
        try:
            written = await asyncio.to_thread(self._store_upload, directory, corpus, testset_csv)
            return await self._repo.create_dataset(
                EvalDataset(
                    id=dataset_id,
                    name=name.strip(),
                    corpus_file_count=written,
                    testset_row_count=len(cases),
                    created_by=user_id,
                )
            )
        except Exception:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            raise

    @staticmethod
    def _read_capped(stream: IO[bytes], limit: int, message: str) -> bytes:
        """Read a stream, refusing anything past ``limit``.

        Reads one byte beyond the cap rather than trusting a client-supplied
        length, so an inflated ``Content-Length`` cannot get past it.
        """
        stream.seek(0)
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValidationError(message, status_code=413)
        return payload

    def _store_upload(
        self,
        directory: Path,
        corpus: Sequence[tuple[str, IO[bytes]]],
        testset_csv: bytes,
    ) -> int:
        """Write the corpus and test set to disk. Blocking; call in a thread."""
        corpus_dir = directory / CORPUS_DIRNAME
        corpus_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        budget = self._settings.max_corpus_bytes
        for filename, stream in corpus:
            # Flatten any path components a browser may have sent.
            target = corpus_dir / Path(filename).name
            if target.exists():
                raise ValidationError(
                    f"Corpus contains more than one file named '{target.name}'.",
                    status_code=400,
                )
            budget -= self._copy_within_budget(stream, target, budget)
            written += 1

        (directory / TESTSET_FILENAME).write_bytes(testset_csv)
        return written

    def _copy_within_budget(self, stream: IO[bytes], target: Path, budget: int) -> int:
        """Copy ``stream`` into ``target``, refusing to exceed ``budget``."""
        stream.seek(0)
        written = 0
        with target.open("wb") as handle:
            while chunk := stream.read(_COPY_CHUNK_BYTES):
                written += len(chunk)
                if written > budget:
                    raise ValidationError(
                        f"Corpus exceeds the {self._settings.max_corpus_mb} MB limit.",
                        status_code=413,
                    )
                handle.write(chunk)
        return written

    async def list_datasets(self) -> list[EvalDataset]:
        return await self._repo.list_datasets()

    async def delete_dataset(self, dataset_id: str) -> None:
        """Delete a dataset and its stored files.

        Refused while a run is using it: the runner reads the corpus from disk
        for the whole indexing phase, so removing it mid-run would surface as a
        confusing FileNotFoundError instead of a clear conflict.
        """
        active = await self._repo.active_run()
        if active is not None and active.dataset_id == dataset_id:
            raise ConflictError(f"Evaluation run '{active.id}' is still using this dataset.")

        if not await self._repo.delete_dataset(dataset_id):
            raise NotFoundError(f"Evaluation dataset '{dataset_id}' not found")
        await asyncio.to_thread(shutil.rmtree, self._dataset_dir(dataset_id), True)

    # ── runs ─────────────────────────────────────────────────────────

    async def list_runs(self, limit: int = 50) -> list[EvalRun]:
        return await self._repo.list_runs(limit)

    async def get_run(self, run_id: str) -> EvalRun:
        run = await self._repo.get_run(run_id)
        if run is None:
            raise NotFoundError(f"Evaluation run '{run_id}' not found")
        return run

    async def start_run(self, dataset_id: str, user_id: int | None) -> EvalRun:
        """Provision a run's partition and token, then dispatch it.

        The run row is inserted before anything is provisioned: the partial
        unique index ``ux_eval_runs_single_active`` makes that insert the mutual
        exclusion between concurrent starts. A read-then-insert would let two
        racing requests both regenerate the shared eval user's token, the second
        revoking the credentials the first is still indexing with.

        Raises:
            NotFoundError: The dataset does not exist.
            ConflictError: Another run is already in flight — the runner
                executes one at a time so timings stay comparable.
        """
        dataset = await self._repo.get_dataset(dataset_id)
        if dataset is None:
            raise NotFoundError(f"Evaluation dataset '{dataset_id}' not found")

        directory = self._dataset_dir(dataset_id)
        testset_path = directory / TESTSET_FILENAME
        if not testset_path.exists():
            raise NotFoundError(f"Test set for dataset '{dataset_id}' is missing on disk")
        cases = parse_testset(testset_path.read_bytes(), max_rows=self._settings.max_testset_rows)

        # Reach the runner before claiming the slot: dispatch is
        # fire-and-forget, so an unreachable actor would otherwise strand the
        # run in QUEUED with a partition and token provisioned for nobody.
        runner = self._runner()
        await self._ping_runner(runner)

        run_id = uuid.uuid4().hex
        partition = eval_partition_name(run_id)
        run = await self._repo.create_run(
            EvalRun(
                id=run_id,
                dataset_id=dataset_id,
                status=EvalRunStatus.QUEUED,
                created_by=user_id,
            )
        )

        try:
            eval_user_id = await self._ensure_eval_user()
            token = (await self._user_service.regenerate_token(eval_user_id))["token"]
            await self._partition_service.create_partition(partition, user_id=eval_user_id)
            self._dispatch(runner, run_id, partition, token, directory, cases)
        except Exception as exc:
            # The run row is the lock; leaving it active would block every
            # later run.
            logger.exception(f"Could not start evaluation run {run_id}: {exc}")
            await self._repo.update_run_status(
                run_id,
                EvalRunStatus.FAILED,
                error=f"Could not start the run: {exc}",
            )
            await self._drop_orphaned_partition(run_id)
            raise

        logger.bind(run_id=run_id, dataset_id=dataset_id).info("Dispatched evaluation run")
        return run

    def _dispatch(
        self,
        runner: Any,
        run_id: str,
        partition: str,
        token: str,
        directory: Path,
        cases: Sequence[Any],
    ) -> None:
        """Hand the run to the worker.

        Fire and forget: the worker owns the run from here and records its own
        outcome, so the ObjectRef is deliberately dropped.
        """
        runner.run.remote(
            run_id=run_id,
            partition=partition,
            token=token,
            api_base_url=self._settings.internal_url,
            corpus_dir=str(directory / CORPUS_DIRNAME),
            cases=[
                {
                    "query": case.query,
                    "expected_answer": case.expected_answer,
                    "expected_file_ids": list(case.expected_file_ids),
                }
                for case in cases
            ],
        )

    async def cancel_run(self, run_id: str) -> EvalRun:
        """Ask the worker to stop, or reap the run if no worker owns it.

        The worker writes the terminal status for a run it is executing. When
        it disowns the run — it restarted, or died before picking the run up —
        nothing else would ever move that row out of an active status, and it
        would block every subsequent run. Cancelling reaps it instead.
        """
        run = await self.get_run(run_id)
        if run.status.is_terminal:
            raise ConflictError(f"Evaluation run '{run_id}' has already finished.")

        from services.workers.ray_utils import call_ray_actor_with_timeout

        owned = False
        try:
            runner = self._runner()
            owned = await call_ray_actor_with_timeout(
                future=runner.cancel.remote(run_id),
                timeout=_RUNNER_PING_TIMEOUT_SECONDS,
                task_description=f"cancelling evaluation run {run_id}",
            )
        except Exception as exc:  # noqa: BLE001 — an unreachable runner still has to be reaped
            logger.warning(f"Evaluation runner unreachable while cancelling {run_id}: {exc}")

        if not owned:
            await self._repo.update_run_status(
                run_id,
                EvalRunStatus.CANCELLED,
                error="No runner owns this run — it was orphaned and has been reaped.",
            )
            await self._drop_orphaned_partition(run_id)
        return await self.get_run(run_id)

    async def _drop_orphaned_partition(self, run_id: str) -> None:
        """Best-effort cleanup of the throwaway partition of a reaped run."""
        try:
            await self._partition_service.delete_partition(eval_partition_name(run_id))
        except Exception as exc:  # noqa: BLE001 — it may never have been created
            logger.debug(f"No eval partition to drop for run {run_id}: {exc}")

    # ── internals ────────────────────────────────────────────────────

    async def _ping_runner(self, runner: Any) -> None:
        """Fail fast when the runner actor cannot start.

        Raises:
            OpenRAGError: The actor is unreachable — surfaced to the caller
                instead of being discovered as a run that never leaves QUEUED.
        """
        from services.workers.ray_utils import call_ray_actor_with_timeout

        try:
            await call_ray_actor_with_timeout(
                future=runner.is_busy.remote(),
                timeout=_RUNNER_PING_TIMEOUT_SECONDS,
                task_description="reaching the evaluation runner",
            )
        except Exception as exc:
            logger.exception(f"Evaluation runner is unavailable: {exc}")
            raise EvaluationRunnerUnavailableError(f"The evaluation runner could not be reached: {exc}") from exc

    def _runner(self) -> Any:
        if self._runner_factory is not None:
            return self._runner_factory()
        from services.workers.eval_runner import build_eval_runner

        return build_eval_runner()

    async def _ensure_eval_user(self) -> int:
        """Get-or-create the non-admin service user runs authenticate as."""
        existing = await self._user_repo.get_user_by_external_id_dict(EVAL_USER_EXTERNAL_ID)
        if existing:
            return int(existing["id"])
        created = await self._user_service.create_user(
            UserCreate(
                display_name=EVAL_USER_DISPLAY_NAME,
                external_user_id=EVAL_USER_EXTERNAL_ID,
                is_admin=False,
                # A corpus is uploaded on every run, so a quota would fail the
                # second one for reasons unrelated to the eval.
                file_quota=-1,
            )
        )
        return int(created["id"])


__all__ = [
    "EVAL_PARTITION_PREFIX",
    "EVAL_USER_EXTERNAL_ID",
    "EvaluationService",
    "eval_partition_name",
    "is_eval_partition",
]
