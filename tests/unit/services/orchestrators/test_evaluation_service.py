"""Tests for EvaluationService dataset storage."""

from __future__ import annotations

import pytest
from core.models.evaluation import EvalDataset, EvalRun, EvalRunStatus
from services.orchestrators.evaluation_service import EvaluationService

DATASET_ID = "ds1"


class FakeRepo:
    def __init__(self, run: EvalRun | None = None) -> None:
        self.deleted_datasets: list[str] = []
        self.dataset = EvalDataset(id=DATASET_ID, name="d", corpus_file_count=1, testset_row_count=1)
        self.run = run

    async def active_run(self):
        if self.run is not None and not self.run.status.is_terminal:
            return self.run
        return None

    async def delete_dataset(self, dataset_id):
        self.deleted_datasets.append(dataset_id)
        return True


def _service(repo, tmp_path=None, settings=None):
    from core.config.root import Settings

    settings = settings or Settings()
    if tmp_path is not None:
        settings = settings.model_copy(update={"paths": settings.paths.model_copy(update={"data_dir": str(tmp_path)})})
    return EvaluationService(repo=repo, config=settings)


def _dataset_on_disk(tmp_path):
    dataset_dir = tmp_path / "eval" / DATASET_ID
    (dataset_dir / "corpus").mkdir(parents=True)
    (dataset_dir / "testset.csv").write_text("question,expected_answer\nq,a\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_deleting_a_dataset_in_use_is_refused(tmp_path):
    """The runner reads the corpus off disk for the whole indexing phase, so
    removing it mid-run would surface as a FileNotFoundError."""
    from core.utils.exceptions import ConflictError

    _dataset_on_disk(tmp_path)
    run = EvalRun(id="run-1", dataset_id=DATASET_ID, status=EvalRunStatus.INDEXING)
    repo = FakeRepo(run=run)
    service = _service(repo, tmp_path=tmp_path)

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
    service = _service(repo, tmp_path=tmp_path)

    await service.delete_dataset(DATASET_ID)

    assert repo.deleted_datasets == [DATASET_ID]
    assert not (tmp_path / "eval" / DATASET_ID).exists()


@pytest.mark.asyncio
async def test_an_oversized_test_set_is_rejected_without_buffering_it_all(tmp_path):
    """The stream is read to one byte past the cap, not to its end."""
    import io

    from core.utils.exceptions import ValidationError

    service = _service(FakeRepo(), tmp_path=tmp_path)
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
