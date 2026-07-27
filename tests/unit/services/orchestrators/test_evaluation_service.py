"""Tests for EvaluationService run dispatch and cancellation.

Both behaviours here were written after a real deployment produced a run that
sat in QUEUED forever: the runner actor had died in its constructor, dispatch
is fire-and-forget so nothing noticed, and cancelling could not clear the row
because no actor claimed it — which blocked every later run.
"""

from __future__ import annotations

import pytest
from core.evaluation.runner import EvaluationRunner
from core.models.evaluation import EvalDataset, EvalRun, EvalRunStatus
from core.utils.exceptions import ConflictError
from services.orchestrators.evaluation_service import (
    EvaluationRunnerUnavailableError,
    EvaluationService,
)

DATASET_ID = "ds1"


class FakeRepo:
    def __init__(self, run: EvalRun | None = None) -> None:
        self.deleted_datasets: list[str] = []
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

    async def active_run(self):
        if self.run is not None and not self.run.status.is_terminal:
            return self.run
        return None

    async def delete_dataset(self, dataset_id):
        self.deleted_datasets.append(dataset_id)
        return True

    async def update_run_status(self, run_id, status, *, error=None):
        self.status_updates.append((run_id, status, error))
        if self.run is not None:
            self.run.status = status
            self.run.error = error


class FakeRunner(EvaluationRunner):
    """In-memory ``EvaluationRunner`` — no Ray, no actor, no worker process."""

    def __init__(self, *, busy_error: Exception | None = None, owns: bool = True) -> None:
        self._busy_error = busy_error
        self._owns = owns
        self.dispatched: dict | None = None

    async def is_busy(self) -> bool:
        if self._busy_error:
            raise self._busy_error
        return False

    async def dispatch(self, **kwargs) -> None:
        self.dispatched = kwargs

    async def cancel(self, run_id: str) -> bool:
        return self._owns


class FakePartitionService:
    def __init__(self, create_error: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self.created: list[str] = []
        self._create_error = create_error

    async def delete_partition(self, partition):
        self.deleted.append(partition)

    async def create_partition(self, partition, user_id=None):
        if self._create_error:
            raise self._create_error
        self.created.append(partition)


class FakeUserRepo:
    """Serves only what the ``UserRepository`` port declares."""

    def __init__(self, user=None) -> None:
        self.user = user

    async def get_user_by_external_id(self, external_id):
        return self.user


class FakeUserService:
    """Counts token regeneration — the side effect the run lock protects."""

    def __init__(self) -> None:
        self.regenerated = 0

    async def regenerate_token(self, user_id):
        self.regenerated += 1
        return {"token": "or-testtoken"}


def _service(
    repo,
    runner,
    partition_service=None,
    tmp_path=None,
    settings=None,
    user_repo=None,
    user_service=None,
):
    from core.config.root import Settings

    settings = settings or Settings()
    if tmp_path is not None:
        settings = settings.model_copy(update={"paths": settings.paths.model_copy(update={"data_dir": str(tmp_path)})})
    return EvaluationService(
        repo=repo,
        runner=runner,
        user_service=user_service or FakeUserService(),
        user_repo=user_repo if user_repo is not None else FakeUserRepo(),
        partition_service=partition_service or FakePartitionService(),
        config=settings,
    )


@pytest.mark.asyncio
async def test_start_run_refuses_when_the_runner_cannot_be_reached(tmp_path):
    """A dead actor must surface as an error, not as a run stuck in QUEUED."""
    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")

    repo = FakeRepo()
    runner = FakeRunner(busy_error=RuntimeError("actor died in __init__"))
    service = _service(repo, runner, tmp_path=tmp_path)

    with pytest.raises(EvaluationRunnerUnavailableError):
        await service.start_run(DATASET_ID, user_id=1)

    assert runner.dispatched is None
    # Nothing was provisioned and no run row was left behind.
    assert repo.run is None


@pytest.mark.asyncio
async def test_dispatch_uses_the_configured_internal_url(tmp_path):
    """The worker's API base URL comes from Settings, not from the environment."""
    from core.config.root import Settings
    from core.models.user import User

    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")

    settings = Settings()
    settings = settings.model_copy(
        update={"server": settings.server.model_copy(update={"internal_url": "http://api.internal:9000"})}
    )
    runner = FakeRunner()
    service = _service(
        FakeRepo(),
        runner,
        tmp_path=tmp_path,
        settings=settings,
        # The eval service user already exists, so it is resolved through the
        # port's ``get_user_by_external_id`` rather than being created.
        user_repo=FakeUserRepo(User(id=7, external_user_id="__openrag_eval__")),
    )

    await service.start_run(DATASET_ID, user_id=1)

    assert runner.dispatched is not None
    assert runner.dispatched["api_base_url"] == "http://api.internal:9000"
    assert runner.dispatched["cases"] == [{"query": "q", "expected_answer": "a", "expected_file_ids": []}]


@pytest.mark.asyncio
async def test_cancel_reaps_a_run_no_runner_owns():
    """Otherwise the orphaned row blocks every subsequent run forever."""
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.QUEUED)
    repo = FakeRepo(run)
    partitions = FakePartitionService()
    service = _service(repo, FakeRunner(owns=False), partition_service=partitions)

    result = await service.cancel_run("r1")

    assert result.status is EvalRunStatus.CANCELLED
    assert "orphaned" in (result.error or "")
    assert partitions.deleted == ["__eval_r1"]


@pytest.mark.asyncio
async def test_cancel_leaves_an_owned_run_for_the_worker_to_finalise():
    """The worker writes its own terminal status, including the metrics."""
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.EVALUATING)
    repo = FakeRepo(run)
    service = _service(repo, FakeRunner(owns=True))

    await service.cancel_run("r1")

    assert repo.status_updates == []


@pytest.mark.asyncio
async def test_cancel_rejects_an_already_finished_run():
    run = EvalRun(id="r1", dataset_id=DATASET_ID, status=EvalRunStatus.COMPLETED)
    service = _service(FakeRepo(run), FakeRunner())

    with pytest.raises(ConflictError):
        await service.cancel_run("r1")


def _dataset_on_disk(tmp_path):
    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_a_second_start_is_refused_before_the_token_is_regenerated(tmp_path):
    """The run row is the lock, so the 409 has to land before provisioning.

    Regenerating the shared eval user's token is what makes a lost race
    destructive: it revokes the credentials the in-flight run is indexing with.
    """
    from core.utils.exceptions import ConflictError

    _dataset_on_disk(tmp_path)

    class BusyRepo(FakeRepo):
        async def create_run(self, run):
            raise ConflictError("An evaluation run is already in progress.")

    repo = BusyRepo()
    runner = FakeRunner()
    users = FakeUserService()
    partitions = FakePartitionService()
    service = _service(repo, runner, partition_service=partitions, tmp_path=tmp_path, user_service=users)

    with pytest.raises(ConflictError):
        await service.start_run(DATASET_ID, user_id=1)

    assert users.regenerated == 0, "the loser must not touch the in-flight run's token"
    assert partitions.created == []
    assert runner.dispatched is None


@pytest.mark.asyncio
async def test_a_failed_provision_releases_the_run_lock(tmp_path):
    """A run left in an active status would block every later run."""
    _dataset_on_disk(tmp_path)

    from core.models.user import User

    repo = FakeRepo()
    runner = FakeRunner()
    partitions = FakePartitionService(create_error=RuntimeError("milvus unreachable"))
    service = _service(
        repo,
        runner,
        partition_service=partitions,
        tmp_path=tmp_path,
        user_repo=FakeUserRepo(User(id=7, external_user_id="__openrag_eval__")),
    )

    with pytest.raises(RuntimeError):
        await service.start_run(DATASET_ID, user_id=1)

    assert runner.dispatched is None
    statuses = [status for _, status, _ in repo.status_updates]
    assert EvalRunStatus.FAILED in statuses, "the lock must be released"
    assert partitions.deleted, "the throwaway partition must not leak"


@pytest.mark.asyncio
async def test_deleting_a_dataset_in_use_is_refused(tmp_path):
    """The runner reads the corpus off disk for the whole indexing phase, so
    removing it mid-run would surface as a FileNotFoundError."""
    from core.utils.exceptions import ConflictError

    _dataset_on_disk(tmp_path)
    run = EvalRun(id="run-1", dataset_id=DATASET_ID, status=EvalRunStatus.INDEXING)
    repo = FakeRepo(run=run)
    service = _service(repo, FakeRunner(), tmp_path=tmp_path)

    with pytest.raises(ConflictError):
        await service.delete_dataset(DATASET_ID)

    assert repo.deleted_datasets == []
    assert (tmp_path / "eval" / DATASET_ID).exists(), "files must survive a refused delete"


@pytest.mark.asyncio
async def test_deleting_a_dataset_an_idle_run_used_is_allowed(tmp_path):
    """Only an *active* run blocks deletion; history keeps its results."""
    _dataset_on_disk(tmp_path)
    run = EvalRun(id="run-1", dataset_id=DATASET_ID, status=EvalRunStatus.COMPLETED)
    repo = FakeRepo(run=run)
    service = _service(repo, FakeRunner(), tmp_path=tmp_path)

    await service.delete_dataset(DATASET_ID)

    assert repo.deleted_datasets == [DATASET_ID]
    assert not (tmp_path / "eval" / DATASET_ID).exists()


@pytest.mark.asyncio
async def test_an_oversized_test_set_is_rejected_without_buffering_it_all(tmp_path):
    """The stream is read to one byte past the cap, not to its end."""
    import io

    from core.utils.exceptions import ValidationError

    service = _service(FakeRepo(), FakeRunner(), tmp_path=tmp_path)
    cap = service._settings.max_testset_bytes
    oversized = io.BytesIO(b"x" * (cap + 5000))

    with pytest.raises(ValidationError) as excinfo:
        await service.create_dataset(
            name="d",
            corpus=[("a.txt", io.BytesIO(b"hello"))],
            testset=oversized,
            user_id=1,
        )
    assert excinfo.value.status_code == 413
    assert oversized.tell() <= cap + 1, "must stop reading once the cap is exceeded"
