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

import os
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
from core.utils.exceptions import ConflictError, NotFoundError, ValidationError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from core.config.root import Settings
    from core.ports.evaluation_repo import EvaluationRepository

logger = get_logger()

#: Stable identity of the service account runs authenticate as.
EVAL_USER_EXTERNAL_ID = "__openrag_eval__"
EVAL_USER_DISPLAY_NAME = "OpenRAG Evaluation"

TESTSET_FILENAME = "testset.csv"
CORPUS_DIRNAME = "corpus"

#: The Ray worker runs in its own container, so it reaches the API by service
#: name rather than through whatever host the admin's browser used.
DEFAULT_INTERNAL_URL = "http://openrag:8080"


def eval_partition_name(run_id: str) -> str:
    return f"{EVAL_PARTITION_PREFIX}{run_id}"


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
        self._runner_factory = runner_factory
        self._root = Path(config.paths.data_dir) / "eval"

    # ── datasets ─────────────────────────────────────────────────────

    def _dataset_dir(self, dataset_id: str) -> Path:
        return self._root / dataset_id

    async def create_dataset(
        self,
        *,
        name: str,
        corpus: Sequence[tuple[str, bytes]],
        testset_csv: bytes,
        user_id: int | None,
    ) -> EvalDataset:
        """Validate and store a corpus + test set.

        The CSV is parsed here so a malformed test set is rejected at upload
        rather than after a run has already spent minutes indexing.
        """
        if not name.strip():
            raise ValidationError("Dataset name is required.", status_code=400)
        if not corpus:
            raise ValidationError("At least one corpus file is required.", status_code=400)

        cases = parse_testset(testset_csv)

        dataset_id = uuid.uuid4().hex
        directory = self._dataset_dir(dataset_id)
        corpus_dir = directory / CORPUS_DIRNAME
        corpus_dir.mkdir(parents=True, exist_ok=True)
        try:
            for filename, payload in corpus:
                # Flatten any path components a browser may have sent.
                (corpus_dir / Path(filename).name).write_bytes(payload)
            (directory / TESTSET_FILENAME).write_bytes(testset_csv)

            return await self._repo.create_dataset(
                EvalDataset(
                    id=dataset_id,
                    name=name.strip(),
                    corpus_file_count=len(corpus),
                    testset_row_count=len(cases),
                    created_by=user_id,
                )
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    async def list_datasets(self) -> list[EvalDataset]:
        return await self._repo.list_datasets()

    async def delete_dataset(self, dataset_id: str) -> None:
        if not await self._repo.delete_dataset(dataset_id):
            raise NotFoundError(f"Evaluation dataset '{dataset_id}' not found")
        shutil.rmtree(self._dataset_dir(dataset_id), ignore_errors=True)

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

        Raises:
            NotFoundError: The dataset does not exist.
            ConflictError: Another run is already in flight — the runner
                executes one at a time so timings stay comparable.
        """
        dataset = await self._repo.get_dataset(dataset_id)
        if dataset is None:
            raise NotFoundError(f"Evaluation dataset '{dataset_id}' not found")

        active = await self._repo.active_run()
        if active is not None:
            raise ConflictError(f"Evaluation run '{active.id}' is already in progress.")

        directory = self._dataset_dir(dataset_id)
        testset_path = directory / TESTSET_FILENAME
        if not testset_path.exists():
            raise NotFoundError(f"Test set for dataset '{dataset_id}' is missing on disk")
        cases = parse_testset(testset_path.read_bytes())

        run_id = uuid.uuid4().hex
        partition = eval_partition_name(run_id)

        eval_user_id = await self._ensure_eval_user()
        token = (await self._user_service.regenerate_token(eval_user_id))["token"]
        await self._partition_service.create_partition(partition, user_id=eval_user_id)

        run = await self._repo.create_run(
            EvalRun(
                id=run_id,
                dataset_id=dataset_id,
                status=EvalRunStatus.QUEUED,
                created_by=user_id,
            )
        )

        # Fire and forget: the worker owns the run from here and records its
        # own outcome, so the ObjectRef is deliberately dropped.
        runner = self._runner()
        runner.run.remote(
            run_id=run_id,
            partition=partition,
            token=token,
            api_base_url=os.getenv("OPENRAG_INTERNAL_URL", DEFAULT_INTERNAL_URL),
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
        logger.bind(run_id=run_id, dataset_id=dataset_id).info("Dispatched evaluation run")
        return run

    async def cancel_run(self, run_id: str) -> EvalRun:
        """Ask the worker to stop; the worker writes the terminal status."""
        run = await self.get_run(run_id)
        if run.status.is_terminal:
            raise ConflictError(f"Evaluation run '{run_id}' has already finished.")

        from services.workers.ray_utils import call_ray_actor_with_timeout

        runner = self._runner()
        await call_ray_actor_with_timeout(
            future=runner.cancel.remote(run_id),
            timeout=30,
            task_description=f"cancelling evaluation run {run_id}",
        )
        return await self.get_run(run_id)

    # ── internals ────────────────────────────────────────────────────

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
                # A corpus is uploaded on every run; a quota would fail the
                # second one for reasons that have nothing to do with the eval.
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
