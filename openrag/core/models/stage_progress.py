"""Stable event and snapshot contracts for per-file indexing progress."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MAX_POSTGRES_INTEGER = 2**31 - 1
_MIN_POSTGRES_INTEGER = -(2**31)


class StageEventStatus(str, Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StageFailureEffect(str, Enum):
    CONTINUED_DEGRADED = "continued_degraded"
    TASK_FAILED = "task_failed"


class StageTaskPhase(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    STAGE = "stage"
    BETWEEN_STAGES = "between_stages"
    FINALIZING = "finalizing"


class StageProgressEvent(BaseModel):
    """One immutable observation of a stage entering or leaving execution."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    task_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1, le=_MAX_POSTGRES_INTEGER)
    sequence: int = Field(ge=1)
    invocation_id: UUID
    stage_id: str = Field(min_length=1, max_length=64)
    stage_rank: int | None = Field(default=None, ge=0, le=_MAX_POSTGRES_INTEGER)
    status: StageEventStatus
    occurred_at: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    failure_effect: StageFailureEffect | None = None

    @field_validator("occurred_at")
    @classmethod
    def require_aware_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_transition_fields(self) -> StageProgressEvent:
        if self.status is StageEventStatus.STARTED and self.duration_ms is not None:
            raise ValueError("duration_ms must be null for a started event")
        if self.failure_effect is not None and self.status is not StageEventStatus.FAILED:
            raise ValueError("failure_effect is valid only for a failed event")
        return self


class StageProgressSummary(BaseModel):
    """Compact stage and recovery state; it contains no durable event tail."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(default=1, ge=1, le=_MAX_POSTGRES_INTEGER)
    latest_sequence: int = Field(default=0, ge=0)
    snapshot_revision: int = Field(default=0, ge=0)
    tracking_version: Literal[1] = 1
    phase: StageTaskPhase = StageTaskPhase.PREPARING
    current_stage_id: str | None = Field(default=None, max_length=64)
    current_stage_rank: int | None = Field(default=None, ge=0, le=_MAX_POSTGRES_INTEGER)
    position_score: int = Field(default=0, ge=_MIN_POSTGRES_INTEGER, le=_MAX_POSTGRES_INTEGER)
    stage_started_at: datetime | None = None
    current_invocation_id: UUID | None = None
    interrupted_invocation_id: UUID | None = None
    expected_final_sequence: int | None = Field(default=None, ge=0)
    known_loss: bool = False
    has_open_invocation: bool = False
    history_truncated: bool = False

    @field_validator("stage_started_at")
    @classmethod
    def normalize_stage_start(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stage_started_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_phase_summary(self) -> StageProgressSummary:
        if self.expected_final_sequence is not None and self.expected_final_sequence < self.latest_sequence:
            raise ValueError("expected_final_sequence cannot precede latest_sequence")
        if self.phase is StageTaskPhase.STAGE and (
            self.current_stage_id is None or self.current_invocation_id is None or not self.has_open_invocation
        ):
            raise ValueError("a stage-phase summary requires an active invocation")
        if self.interrupted_invocation_id is not None and (
            not self.has_open_invocation or self.interrupted_invocation_id != self.current_invocation_id
        ):
            raise ValueError("an interrupted invocation must identify the open stage invocation")
        return self


class DurableStageProgress(StageProgressSummary):
    """Persisted projection, including writer-owned coverage evidence."""

    persisted_through_sequence: int = Field(default=0, ge=0)
    known_conflict: bool = False


class StageProgressCoverage(BaseModel):
    task_id: str
    attempt: int = Field(ge=1, le=_MAX_POSTGRES_INTEGER)
    persisted_through_sequence: int = Field(ge=0)


class StageProgressEventCursor(BaseModel):
    attempt: int = Field(ge=1, le=_MAX_POSTGRES_INTEGER)
    sequence: int = Field(ge=1)


class StageProgressEventPage(BaseModel):
    events: list[StageProgressEvent] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: StageProgressEventCursor | None = None


class StageProgressWriteResult(BaseModel):
    """Bounded outcome summary for one best-effort repository batch."""

    coverage: list[StageProgressCoverage] = Field(default_factory=list)
    missing_parent_task_ids: list[str] = Field(default_factory=list)
    conflicted_task_ids: list[str] = Field(default_factory=list)


class StageProgressWorkerSnapshot(BaseModel):
    """Live worker view; elapsed samples never become durable stage events."""

    model_config = ConfigDict(extra="forbid")

    summary: StageProgressSummary
    event_tail: list[StageProgressEvent] = Field(default_factory=list, max_length=32)
    active_invocation_id: UUID | None = None
    elapsed_sampled_at: datetime | None = None
    elapsed_ms: int | None = Field(default=None, ge=0)

    @field_validator("elapsed_sampled_at")
    @classmethod
    def normalize_elapsed_sample_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("elapsed_sampled_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_elapsed_sample(self) -> StageProgressWorkerSnapshot:
        if self.elapsed_ms is not None and self.active_invocation_id is None:
            raise ValueError("elapsed_ms requires an active invocation")
        return self
