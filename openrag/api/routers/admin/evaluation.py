"""Admin routes for the evaluation page.

Datasets are uploaded once and replayed by runs. Every route is admin-only:
a run indexes a corpus, spends grader tokens, and occupies the single runner
slot, so it is not something a partition editor should be able to trigger.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from api.dependencies.auth import current_user, require_admin
from api.schemas.admin.evaluation_schemas import (
    EvalDatasetResponse,
    EvalRunResponse,
    EvalRunSummaryResponse,
    StartRunRequest,
)
from di.providers import get_evaluation_service
from fastapi import APIRouter, Depends, File, Form, UploadFile, status

router = APIRouter(dependencies=[Depends(require_admin)])


def _run_summary(run: Any) -> EvalRunSummaryResponse:
    return EvalRunSummaryResponse(
        id=run.id,
        dataset_id=run.dataset_id,
        status=run.status.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        hit_rate=run.retrieval.hit_rate if run.retrieval else None,
        mrr=run.retrieval.mrr if run.retrieval else None,
        answer_pass_rate=run.answer.pass_rate if run.answer else None,
        files_per_minute=run.indexing.files_per_minute if run.indexing else None,
        error=run.error,
    )


def _run_detail(run: Any) -> EvalRunResponse:
    return EvalRunResponse(
        id=run.id,
        dataset_id=run.dataset_id,
        status=run.status.value,
        started_at=run.started_at,
        finished_at=run.finished_at,
        indexing=asdict(run.indexing) if run.indexing else None,
        retrieval=asdict(run.retrieval) if run.retrieval else None,
        answer=asdict(run.answer) if run.answer else None,
        cases=[asdict(case) for case in run.cases],
        error=run.error,
        created_by=run.created_by,
    )


@router.get("/datasets", response_model=list[EvalDatasetResponse])
async def list_datasets(service=Depends(get_evaluation_service)):
    """List stored evaluation datasets, newest first."""
    return [asdict(dataset) for dataset in await service.list_datasets()]


@router.post(
    "/datasets",
    response_model=EvalDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset(
    name: str = Form(..., description="Human-readable dataset name"),
    testset: UploadFile = File(..., description="CSV: question,expected_answer,expected_file_ids"),
    corpus: list[UploadFile] = File(..., description="Documents to index for the run"),
    user=Depends(current_user),
    service=Depends(get_evaluation_service),
):
    """Upload a corpus and its test set.

    The CSV is validated here, so a bad test set fails now rather than after a
    run has already indexed the corpus.
    """
    # Pass the open streams, not the bytes: large uploads are already spooled
    # to disk, and reading them here would pull them into memory unbounded.
    dataset = await service.create_dataset(
        name=name,
        corpus=[(upload.filename or "unnamed", upload.file) for upload in corpus],
        testset=testset.file,
        user_id=user.get("id") if isinstance(user, dict) else None,
    )
    return asdict(dataset)


@router.delete("/datasets/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(dataset_id: str, service=Depends(get_evaluation_service)):
    """Delete a dataset and its stored files."""
    await service.delete_dataset(dataset_id)


@router.get("/runs", response_model=list[EvalRunSummaryResponse])
async def list_runs(limit: int = 50, service=Depends(get_evaluation_service)):
    """Run history, newest first."""
    return [_run_summary(run) for run in await service.list_runs(limit)]


@router.post("/runs", response_model=EvalRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    body: StartRunRequest,
    user=Depends(current_user),
    service=Depends(get_evaluation_service),
):
    """Queue a run against a dataset.

    Returns ``409`` when a run is already in flight — runs execute one at a
    time so that indexing timings stay comparable between them.
    """
    run = await service.start_run(
        body.dataset_id,
        user.get("id") if isinstance(user, dict) else None,
    )
    return _run_detail(run)


@router.get("/runs/{run_id}", response_model=EvalRunResponse)
async def get_run(run_id: str, service=Depends(get_evaluation_service)):
    """One run with its metrics and per-question detail."""
    return _run_detail(await service.get_run(run_id))


@router.post("/runs/{run_id}/cancel", response_model=EvalRunResponse)
async def cancel_run(run_id: str, service=Depends(get_evaluation_service)):
    """Ask the runner to abandon an in-flight run."""
    return _run_detail(await service.cancel_run(run_id))
