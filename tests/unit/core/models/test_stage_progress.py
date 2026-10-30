"""Validation rules for immutable indexing-stage observations."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module, util
from uuid import uuid4

import pytest
from pydantic import ValidationError


def _stage_models():
    spec = util.find_spec("core.models.stage_progress")
    assert spec is not None, "the stage-progress contract is not defined"
    return import_module("core.models.stage_progress")


def test_stage_event_requires_utc_aware_time_and_nonnegative_duration() -> None:
    models = _stage_models()
    event = {
        "event_id": uuid4(),
        "task_id": "task-1",
        "attempt": 1,
        "sequence": 1,
        "invocation_id": uuid4(),
        "stage_id": "parse",
        "stage_rank": 0,
        "status": "started",
        "occurred_at": datetime.now(UTC),
    }

    with pytest.raises(ValidationError, match="timezone-aware"):
        models.StageProgressEvent(**(event | {"occurred_at": datetime(2026, 10, 22)}))
    with pytest.raises(ValidationError):
        models.StageProgressEvent(**(event | {"duration_ms": -1}))


def test_stage_end_allows_unknown_duration_and_start_cannot_have_one() -> None:
    models = _stage_models()
    base = {
        "event_id": uuid4(),
        "task_id": "task-1",
        "attempt": 1,
        "sequence": 2,
        "invocation_id": uuid4(),
        "stage_id": "parse",
        "stage_rank": 0,
        "occurred_at": datetime.now(UTC),
    }

    unknown_duration = models.StageProgressEvent(**(base | {"status": "completed"}))
    assert unknown_duration.duration_ms is None
    with pytest.raises(ValidationError, match="duration_ms"):
        models.StageProgressEvent(**(base | {"status": "started", "duration_ms": 12}))


def test_stage_event_rejects_values_outside_postgres_integer_range() -> None:
    models = _stage_models()
    event = {
        "task_id": "task-1",
        "attempt": 1,
        "sequence": 1,
        "invocation_id": uuid4(),
        "stage_id": "parse",
        "status": "started",
        "occurred_at": datetime.now(UTC),
    }

    for field in ("attempt", "stage_rank"):
        with pytest.raises(ValidationError):
            models.StageProgressEvent(**(event | {field: 2**31}))

    maximum = models.StageProgressEvent(**(event | {"attempt": 2**31 - 1, "stage_rank": 2**31 - 1}))
    assert maximum.attempt == 2**31 - 1
    assert maximum.stage_rank == 2**31 - 1


def test_stage_summary_rejects_values_outside_postgres_integer_range() -> None:
    models = _stage_models()
    summary = {"task_id": "task-1"}

    for field in ("attempt", "current_stage_rank"):
        with pytest.raises(ValidationError):
            models.StageProgressSummary(**(summary | {field: 2**31}))
    with pytest.raises(ValidationError):
        models.StageProgressSummary(**(summary | {"position_score": 2**31}))
    with pytest.raises(ValidationError):
        models.StageProgressSummary(**(summary | {"position_score": -(2**31) - 1}))

    minimum_score = models.StageProgressSummary(**(summary | {"position_score": -(2**31)}))
    maximum_score = models.StageProgressSummary(**(summary | {"position_score": 2**31 - 1}))
    assert minimum_score.position_score == -(2**31)
    assert maximum_score.position_score == 2**31 - 1


def test_stage_progress_cursors_and_coverage_reject_attempt_overflow() -> None:
    models = _stage_models()

    with pytest.raises(ValidationError):
        models.StageProgressCoverage(task_id="task-1", attempt=2**31, persisted_through_sequence=0)
    with pytest.raises(ValidationError):
        models.StageProgressEventCursor(attempt=2**31, sequence=1)
