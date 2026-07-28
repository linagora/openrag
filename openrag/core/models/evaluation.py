"""Domain models for the evaluation feature.

An *evaluation dataset* pairs a corpus (the files to index) with a test set
(the questions to ask). An *evaluation run* indexes that corpus into a
throwaway partition, replays the test set against the live API through
promptfoo, and records three families of metrics: indexing speed, retrieval
quality, and answer quality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

#: Runs index their corpus into a throwaway partition named from the run id.
#: These are an implementation detail of an eval and are filtered out of the
#: partition listings so they never show up as user-facing collections.
EVAL_PARTITION_PREFIX = "__eval_"


def is_eval_partition(partition: str) -> bool:
    """True for the throwaway partition a run creates."""
    return partition.startswith(EVAL_PARTITION_PREFIX)


class EvalRunStatus(str, Enum):
    """Lifecycle of a single evaluation run.

    Mirrors the indexing task vocabulary (``services.workers.task_state``) so
    the admin UI can reuse the same status styling.
    """

    QUEUED = "QUEUED"
    INDEXING = "INDEXING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in (EvalRunStatus.COMPLETED, EvalRunStatus.FAILED, EvalRunStatus.CANCELLED)


@dataclass(frozen=True)
class EvalTestCase:
    """One row of the uploaded test set CSV.

    ``expected_file_ids`` is optional: rows without it still contribute to the
    answer-quality metrics, but are excluded from hit rate / MRR / recall
    rather than being counted as misses.
    """

    query: str
    expected_answer: str
    expected_file_ids: tuple[str, ...] = ()

    @property
    def has_ground_truth_sources(self) -> bool:
        return bool(self.expected_file_ids)


@dataclass
class EvalDataset:
    """A stored corpus + test set pair."""

    id: str
    name: str
    corpus_file_count: int
    testset_row_count: int
    created_at: datetime | None = None
    created_by: int | None = None


@dataclass
class FileIndexingSample:
    """Wall-clock cost of indexing one corpus file."""

    filename: str
    size_bytes: int
    duration_seconds: float
    failed: bool = False


@dataclass
class IndexingMetrics:
    """Aggregate indexing speed over a run's corpus."""

    files_total: int = 0
    files_failed: int = 0
    bytes_total: int = 0
    wall_seconds: float = 0.0
    files_per_minute: float = 0.0
    megabytes_per_second: float = 0.0
    p50_seconds: float = 0.0
    p95_seconds: float = 0.0
    by_extension: dict[str, dict[str, float]] = field(default_factory=dict)
    samples: list[FileIndexingSample] = field(default_factory=list)


@dataclass
class RetrievalMetrics:
    """Ranking quality of the retrieved chunks.

    ``scored_cases`` counts the test rows that carried ``expected_file_ids``;
    ``skipped_cases`` counts those that did not. Both are reported so a
    near-empty ground truth can never masquerade as a perfect score.
    """

    scored_cases: int = 0
    skipped_cases: int = 0
    hit_rate: float = 0.0
    mrr: float = 0.0
    recall: float = 0.0
    context_relevance: float | None = None


@dataclass
class AnswerMetrics:
    """LLM-graded quality of the generated answers."""

    scored_cases: int = 0
    pass_rate: float = 0.0
    factuality: float | None = None
    rubric_score: float | None = None


@dataclass
class EvalCaseResult:
    """Per-question detail surfaced in the run detail table."""

    query: str
    retrieved_file_ids: list[str] = field(default_factory=list)
    expected_file_ids: list[str] = field(default_factory=list)
    hit: bool | None = None
    reciprocal_rank: float | None = None
    answer: str | None = None
    answer_passed: bool | None = None
    grader_reason: str | None = None


@dataclass
class EvalRun:
    """One evaluation execution against a dataset."""

    id: str
    dataset_id: str
    status: EvalRunStatus = EvalRunStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    indexing: IndexingMetrics | None = None
    retrieval: RetrievalMetrics | None = None
    answer: AnswerMetrics | None = None
    cases: list[EvalCaseResult] = field(default_factory=list)
    error: str | None = None
    created_by: int | None = None


__all__ = [
    "EVAL_PARTITION_PREFIX",
    "AnswerMetrics",
    "EvalCaseResult",
    "EvalDataset",
    "EvalRun",
    "EvalRunStatus",
    "EvalTestCase",
    "FileIndexingSample",
    "IndexingMetrics",
    "RetrievalMetrics",
    "is_eval_partition",
]
