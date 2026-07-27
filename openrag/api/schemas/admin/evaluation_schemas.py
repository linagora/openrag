"""Response models for the admin evaluation endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class EvalDatasetResponse(BaseModel):
    """A stored corpus + test set."""

    id: str
    name: str
    corpus_file_count: int
    testset_row_count: int
    created_at: datetime | None = None
    created_by: int | None = None


class FileIndexingSampleResponse(BaseModel):
    filename: str
    size_bytes: int
    duration_seconds: float
    failed: bool = False


class IndexingMetricsResponse(BaseModel):
    files_total: int
    files_failed: int
    bytes_total: int
    wall_seconds: float
    files_per_minute: float
    megabytes_per_second: float
    p50_seconds: float
    p95_seconds: float
    by_extension: dict[str, dict[str, float]] = Field(default_factory=dict)
    samples: list[FileIndexingSampleResponse] = Field(default_factory=list)


class RetrievalMetricsResponse(BaseModel):
    scored_cases: int
    skipped_cases: int
    hit_rate: float
    mrr: float
    recall: float
    context_relevance: float | None = None


class AnswerMetricsResponse(BaseModel):
    scored_cases: int
    pass_rate: float
    factuality: float | None = None
    rubric_score: float | None = None


class EvalCaseResponse(BaseModel):
    query: str
    retrieved_file_ids: list[str] = Field(default_factory=list)
    expected_file_ids: list[str] = Field(default_factory=list)
    hit: bool | None = None
    reciprocal_rank: float | None = None
    answer: str | None = None
    answer_passed: bool | None = None
    grader_reason: str | None = None


class EvalRunResponse(BaseModel):
    """A run, with metrics once it has finished."""

    id: str
    dataset_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    indexing: IndexingMetricsResponse | None = None
    retrieval: RetrievalMetricsResponse | None = None
    answer: AnswerMetricsResponse | None = None
    cases: list[EvalCaseResponse] = Field(default_factory=list)
    error: str | None = None
    created_by: int | None = None


class EvalRunSummaryResponse(BaseModel):
    """Run history row — metrics headline only, no per-case detail."""

    id: str
    dataset_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    hit_rate: float | None = None
    mrr: float | None = None
    answer_pass_rate: float | None = None
    files_per_minute: float | None = None
    error: str | None = None


class StartRunRequest(BaseModel):
    dataset_id: str


__all__ = [
    "AnswerMetricsResponse",
    "EvalCaseResponse",
    "EvalDatasetResponse",
    "EvalRunResponse",
    "EvalRunSummaryResponse",
    "FileIndexingSampleResponse",
    "IndexingMetricsResponse",
    "RetrievalMetricsResponse",
    "StartRunRequest",
]
