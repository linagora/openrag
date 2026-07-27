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
        self.dataset = EvalDataset(id=DATASET_ID, name="d", corpus_file_count=1, testset_row_count=1)
        self.run = run
        self.status_updates: list[tuple[str, EvalRunStatus, str | None]] = []

    async def get_dataset(self, dataset_id):
        return self.dataset if dataset_id == DATASET_ID else None

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
    def __init__(self, create_error: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self.created: list[str] = []
        self._create_error = create_error

    async def create_partition(self, partition, user_id=None):
        if self._create_error:
            raise self._create_error
        self.created.append(partition)

    async def delete_partition(self, partition):
        self.deleted.append(partition)


class FakeUserService:
    """Records token regeneration — the side effect the run lock protects."""

    def __init__(self) -> None:
        self.regenerated = 0

    async def regenerate_token(self, user_id):
        self.regenerated += 1
        return {"token": "or-fake"}


class FakeUserRepo:
    async def get_user_by_external_id_dict(self, external_user_id):
        return {"id": 7}


def _service(repo, runner, partition_service=None, tmp_path=None, user_service=None):
    from core.config.root import Settings

    settings = Settings()
    if tmp_path is not None:
        settings = settings.model_copy(update={"paths": settings.paths.model_copy(update={"data_dir": str(tmp_path)})})
    return EvaluationService(
        repo=repo,
        user_service=user_service or FakeUserService(),
        user_repo=FakeUserRepo(),
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


def _dataset_on_disk(tmp_path):
    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_a_second_start_is_refused_before_the_token_is_regenerated(tmp_path):
    """The run row is the lock, so the 409 has to land *before* provisioning.

    Regenerating the shared eval user's token is what makes a lost race
    destructive: it would revoke the credentials the in-flight run is still
    indexing with.
    """
    from core.utils.exceptions import ConflictError

    _dataset_on_disk(tmp_path)

    class BusyRepo(FakeRepo):
        async def create_run(self, run):
            raise ConflictError("An evaluation run is already in progress.")

    repo = BusyRepo()
    runner = FakeRunnerHandle()
    users = FakeUserService()
    partitions = FakePartitionService()
    service = _service(repo, runner, partition_service=partitions, tmp_path=tmp_path, user_service=users)

    with pytest.raises(ConflictError):
        await service.start_run(DATASET_ID, user_id=1)

    assert users.regenerated == 0, "the loser must not touch the in-flight run's token"
    assert partitions.created == []
    assert runner.dispatched is False


@pytest.mark.asyncio
async def test_a_failed_provision_releases_the_run_lock(tmp_path):
    """A run left in an active status would block every later run forever."""
    _dataset_on_disk(tmp_path)

    repo = FakeRepo()
    runner = FakeRunnerHandle()
    partitions = FakePartitionService(create_error=RuntimeError("milvus unreachable"))
    service = _service(repo, runner, partition_service=partitions, tmp_path=tmp_path)

    with pytest.raises(RuntimeError):
        await service.start_run(DATASET_ID, user_id=1)

    assert runner.dispatched is False
    statuses = [status for _, status, _ in repo.status_updates]
    assert EvalRunStatus.FAILED in statuses, "the lock must be released"
    assert partitions.deleted, "the throwaway partition must not leak"
