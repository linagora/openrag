"""Metric computation for an evaluation run.

Two jobs live here, both pure:

* aggregate the per-file indexing timings the worker collected;
* turn promptfoo's ``results.json`` into ranking and answer-quality numbers.

The ranking definitions (hit rate, MRR, recall) follow the write-up in
``tests/load/automatic-evaluation-pipeline/README.md`` so the numbers this
page reports mean the same thing as the ones the offline pipeline produced.

promptfoo's output envelope has shifted across releases, so
:func:`extract_results` accepts either the ``{"results": {"results": [...]}}``
v3 shape or a bare list, and every field read from a row is treated as
optional.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.models.evaluation import (
    AnswerMetrics,
    EvalCaseResult,
    EvalTestCase,
    FileIndexingSample,
    IndexingMetrics,
    RetrievalMetrics,
)

_BYTES_PER_MB = 1024 * 1024


def _mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile.

    ``statistics.quantiles`` needs at least two points and interpolates;
    nearest-rank keeps a single-file run meaningful and always returns a
    duration that was actually observed.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * len(ordered) + 0.5) - 1))
    return float(ordered[index])


def indexing_metrics(samples: Sequence[FileIndexingSample], wall_seconds: float) -> IndexingMetrics:
    """Aggregate per-file timings into throughput figures.

    ``wall_seconds`` is the measured end-to-end duration of the indexing
    phase, which is what throughput is derived from — summing per-file
    durations would overstate speed whenever files are indexed concurrently.
    """
    succeeded = [s for s in samples if not s.failed]
    durations = [s.duration_seconds for s in succeeded]
    total_bytes = sum(s.size_bytes for s in succeeded)

    by_extension: dict[str, dict[str, float]] = {}
    for sample in succeeded:
        extension = (Path(sample.filename).suffix or "(none)").lower()
        bucket = by_extension.setdefault(extension, {"files": 0.0, "seconds": 0.0})
        bucket["files"] += 1
        bucket["seconds"] += sample.duration_seconds
    for bucket in by_extension.values():
        bucket["mean_seconds"] = round(bucket["seconds"] / bucket["files"], 3)

    return IndexingMetrics(
        files_total=len(samples),
        files_failed=sum(1 for s in samples if s.failed),
        bytes_total=total_bytes,
        wall_seconds=round(wall_seconds, 3),
        files_per_minute=round(len(succeeded) / wall_seconds * 60, 2) if wall_seconds > 0 else 0.0,
        megabytes_per_second=(round(total_bytes / _BYTES_PER_MB / wall_seconds, 3) if wall_seconds > 0 else 0.0),
        p50_seconds=round(_percentile(durations, 0.50), 3),
        p95_seconds=round(_percentile(durations, 0.95), 3),
        by_extension=by_extension,
        samples=list(samples),
    )


def extract_results(payload: Any) -> list[dict[str, Any]]:
    """Pull the per-test rows out of a promptfoo output file."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    results = payload.get("results")
    if isinstance(results, Mapping):
        results = results.get("results")
    if isinstance(results, list):
        return [row for row in results if isinstance(row, Mapping)]
    return []


def _row_query(row: Mapping[str, Any]) -> str:
    variables = row.get("vars")
    if isinstance(variables, Mapping):
        return str(variables.get("query", ""))
    return ""


def _row_output(row: Mapping[str, Any]) -> Any:
    response = row.get("response")
    if isinstance(response, Mapping) and "output" in response:
        return response["output"]
    return row.get("output")


def _index_by_query(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Map each question to its row, keeping the first when a query repeats."""
    indexed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        query = _row_query(row)
        if query and query not in indexed:
            indexed[query] = row
    return indexed


def _retrieved_file_ids(output: Any) -> list[str]:
    """Rank-ordered ``file_id``s from a ``/search`` response."""
    if not isinstance(output, list):
        return []
    file_ids: list[str] = []
    for document in output:
        if not isinstance(document, Mapping):
            continue
        metadata = document.get("metadata")
        file_id = metadata.get("file_id") if isinstance(metadata, Mapping) else None
        if file_id:
            file_ids.append(str(file_id))
    return file_ids


def _grading_score(row: Mapping[str, Any], assertion_type: str | None = None) -> float | None:
    """Score for a row, optionally narrowed to one assertion type."""
    grading = row.get("gradingResult")
    if not isinstance(grading, Mapping):
        return None
    if assertion_type is None:
        score = grading.get("score")
        return float(score) if isinstance(score, int | float) else None

    components = grading.get("componentResults")
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, Mapping):
            continue
        assertion = component.get("assertion")
        if isinstance(assertion, Mapping) and assertion.get("type") == assertion_type:
            score = component.get("score")
            if isinstance(score, int | float):
                return float(score)
    return None


def _grading_reason(row: Mapping[str, Any]) -> str | None:
    grading = row.get("gradingResult")
    if isinstance(grading, Mapping):
        reason = grading.get("reason")
        return str(reason) if reason else None
    return None


def summarize(
    *,
    cases: Sequence[EvalTestCase],
    retrieval_payload: Any,
    answer_payload: Any,
) -> tuple[RetrievalMetrics, AnswerMetrics, list[EvalCaseResult]]:
    """Fold both promptfoo outputs into metrics plus per-question detail.

    Test cases with no ``expected_file_ids`` are counted in ``skipped_cases``
    and left out of hit rate / MRR / recall — scoring them as misses would
    make a sparsely-annotated test set look like a broken retriever.
    """
    retrieval_rows = _index_by_query(extract_results(retrieval_payload))
    answer_rows = _index_by_query(extract_results(answer_payload))

    hits: list[float] = []
    reciprocal_ranks: list[float] = []
    recalls: list[float] = []
    relevance_scores: list[float] = []
    answer_passes: list[float] = []
    factuality_scores: list[float] = []
    rubric_scores: list[float] = []
    details: list[EvalCaseResult] = []

    for case in cases:
        retrieval_row = retrieval_rows.get(case.query)
        answer_row = answer_rows.get(case.query)

        retrieved = _retrieved_file_ids(_row_output(retrieval_row)) if retrieval_row else []
        detail = EvalCaseResult(
            query=case.query,
            retrieved_file_ids=retrieved,
            expected_file_ids=list(case.expected_file_ids),
        )

        if retrieval_row is not None:
            relevance = _grading_score(retrieval_row, "context-relevance")
            if relevance is not None:
                relevance_scores.append(relevance)

        if case.has_ground_truth_sources:
            expected = set(case.expected_file_ids)
            matched = [rank for rank, fid in enumerate(retrieved, start=1) if fid in expected]
            detail.hit = bool(matched)
            detail.reciprocal_rank = 1.0 / matched[0] if matched else 0.0
            hits.append(1.0 if matched else 0.0)
            reciprocal_ranks.append(detail.reciprocal_rank)
            recalls.append(len(expected & set(retrieved)) / len(expected))

        if answer_row is not None:
            output = _row_output(answer_row)
            if isinstance(output, Mapping):
                output = output.get("answer")
            detail.answer = str(output) if output is not None else None
            detail.answer_passed = bool(answer_row.get("success"))
            detail.grader_reason = _grading_reason(answer_row)
            answer_passes.append(1.0 if detail.answer_passed else 0.0)
            for assertion_type, sink in (
                ("factuality", factuality_scores),
                ("llm-rubric", rubric_scores),
            ):
                score = _grading_score(answer_row, assertion_type)
                if score is not None:
                    sink.append(score)

        details.append(detail)

    scored = len(hits)
    retrieval = RetrievalMetrics(
        scored_cases=scored,
        skipped_cases=len(cases) - scored,
        hit_rate=round(_mean(hits), 4),
        mrr=round(_mean(reciprocal_ranks), 4),
        recall=round(_mean(recalls), 4),
        context_relevance=round(_mean(relevance_scores), 4) if relevance_scores else None,
    )
    answer = AnswerMetrics(
        scored_cases=len(answer_passes),
        pass_rate=round(_mean(answer_passes), 4),
        factuality=round(_mean(factuality_scores), 4) if factuality_scores else None,
        rubric_score=round(_mean(rubric_scores), 4) if rubric_scores else None,
    )
    return retrieval, answer, details


__all__ = ["extract_results", "indexing_metrics", "summarize"]
