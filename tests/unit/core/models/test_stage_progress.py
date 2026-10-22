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
