"""Tests for EvaluationService run dispatch and cancellation.

Both behaviours here were written after a real deployment produced a run that
sat in QUEUED forever: the runner actor had died in its constructor, dispatch
is fire-and-forget so nothing noticed, and cancelling could not clear the row
because no actor claimed it — which blocked every later run.
"""

from __future__ import annotations

import sys
import types

import pytest
from core.models.evaluation import EvalDataset, EvalRun, EvalRunStatus
from core.utils.exceptions import ConflictError
from services.orchestrators.evaluation_service import (
    EvaluationRunnerUnavailableError,
    EvaluationService,
)

DATASET_ID = "ds1"


@pytest.fixture(autouse=True)
def _stub_ray_utils(monkeypatch):
    """Same stub the job-service tests use — ray itself is not needed here."""

    async def _call_ray_actor_with_timeout(*, future, timeout, task_description):
        return await future

    ray_utils = types.ModuleType("services.workers.ray_utils")
    ray_utils.call_ray_actor_with_timeout = _call_ray_actor_with_timeout
    monkeypatch.setitem(sys.modules, "services.workers.ray_utils", ray_utils)


class FakeRepo:
    def __init__(self, run: EvalRun | None = None) -> None:
        self.dataset = EvalDataset(
            id=DATASET_ID, name="d", corpus_file_count=1, testset_row_count=1
        )
        self.run = run
        self.status_updates: list[tuple[str, EvalRunStatus, str | None]] = []

    async def get_dataset(self, dataset_id):
        return self.dataset if dataset_id == DATASET_ID else None

    async def active_run(self):
        return None

    async def create_run(self, run):
        self.run = run
        return run

    async def get_run(self, run_id):
        return self.run

    async def update_run_status(self, run_id, status, *, error=None):
        self.status_updates.append((run_id, status, error))
        if self.run is not None:
            self.run.status = status
            self.run.error = error


class FakeRunnerHandle:
    """Stands in for the Ray actor handle; `.remote()` returns an awaitable."""

    def __init__(self, *, busy_error: Exception | None = None, owns: bool = True) -> None:
        self._busy_error = busy_error
        self._owns = owns
        self.dispatched = False

    class _Method:
        def __init__(self, fn):
            self._fn = fn

        def remote(self, *args, **kwargs):
            return self._fn(*args, **kwargs)

    @property
    def is_busy(self):
        async def _call():
            if self._busy_error:
                raise self._busy_error
            return False

        return self._Method(lambda: _call())

    @property
    def cancel(self):
        async def _call(_run_id):
            return self._owns

        return self._Method(_call)

    @property
    def run(self):
        def _call(**_kwargs):
            self.dispatched = True

        return self._Method(_call)


class FakePartitionService:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_partition(self, partition):
        self.deleted.append(partition)


def _service(repo, runner, partition_service=None, tmp_path=None):
    from core.config.root import Settings

    settings = Settings()
    if tmp_path is not None:
        settings = settings.model_copy(
            update={"paths": settings.paths.model_copy(update={"data_dir": str(tmp_path)})}
        )
    return EvaluationService(
        repo=repo,
        user_service=object(),
        user_repo=object(),
        partition_service=partition_service or FakePartitionService(),
        config=settings,
        runner_factory=lambda: runner,
    )


@pytest.mark.asyncio
async def test_start_run_refuses_when_the_runner_cannot_be_reached(tmp_path):
    """A dead actor must surface as an error, not as a run stuck in QUEUED."""
    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")

    repo = FakeRepo()
    runner = FakeRunnerHandle(busy_error=RuntimeError("actor died in __init__"))
    service = _service(repo, runner, tmp_path=tmp_path)

    with pytest.raises(EvaluationRunnerUnavailableError):
        await service.start_run(DATASET_ID, user_id=1)

    assert runner.dispatched is False
    # Nothing was provisioned and no run row was left behind.
    assert repo.run is None


@pytest.mark.asyncio
async def test_cancel_reaps_a_run_no_runner_owns():
    """Otherwise the orphaned row blocks every subsequent run forever."""
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.QUEUED)
    repo = FakeRepo(run)
    partitions = FakePartitionService()
    service = _service(repo, FakeRunnerHandle(owns=False), partition_service=partitions)

    result = await service.cancel_run("r1")

    assert result.status is EvalRunStatus.CANCELLED
    assert "orphaned" in (result.error or "")
    assert partitions.deleted == ["__eval_r1"]


@pytest.mark.asyncio
async def test_cancel_leaves_an_owned_run_for_the_worker_to_finalise():
    """The worker writes its own terminal status, including the metrics."""
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.EVALUATING)
    repo = FakeRepo(run)
    service = _service(repo, FakeRunnerHandle(owns=True))

    await service.cancel_run("r1")

    assert repo.status_updates == []


@pytest.mark.asyncio
async def test_cancel_rejects_an_already_finished_run():
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.COMPLETED)
    service = _service(FakeRepo(run), FakeRunnerHandle())

    with pytest.raises(ConflictError):
        await service.cancel_run("r1")
