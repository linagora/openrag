"""Catalog domain models — document records, indexation jobs, status tracking."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentStatus(str, Enum):
    QUEUED = "QUEUED"
    SERIALIZING = "SERIALIZING"
    CHUNKING = "CHUNKING"
    INSERTING = "INSERTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# States a task/document cannot leave once reached. Single source of truth for
# the cancellation guard shared by the indexing route, dispatcher, and
# TaskStateManager — keep those in sync via this constant rather than
# re-declaring the set.
TERMINAL_TASK_STATES = frozenset({DocumentStatus.COMPLETED, DocumentStatus.FAILED, DocumentStatus.CANCELLED})


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class DocumentRecord(BaseModel):
    """A document entry in the catalog (PostgreSQL)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    file_id: str = ""
    filename: str = ""
    partition: str = "default"
    metadata: dict[str, Any] = Field(default_factory=dict)
    indexation_config: dict[str, Any] | None = None
    status: DocumentStatus = DocumentStatus.QUEUED
    error_message: str | None = None
    created_by: int | None = None
    relationship_id: str | None = None
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IndexationJob(BaseModel):
    """A durable indexation job — one row per dispatched indexing task.

    The record mirrors what the in-memory ``TaskStateManager`` Ray actor holds,
    but survives restarts and is operator-visible (issue #660). ``id`` is the
    dispatcher's ``task_id``, so the durable row and the hot-cache entry share
    one identity.

    ``status`` reuses :class:`DocumentStatus` rather than :class:`JobStatus`
    because a job here tracks exactly one file and must reproduce the actor's
    state taxonomy verbatim (``QUEUED`` -> ``SERIALIZING`` -> ... ->
    ``COMPLETED`` / ``FAILED`` / ``CANCELLED``); :class:`JobStatus` is the
    coarser roll-up reserved for a future batch-level job entity.

    ``error`` is bounded at write time (see
    :func:`core.utils.text.truncate_error_text`) — a full traceback is
    unbounded input and this row is retained.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: DocumentStatus = DocumentStatus.QUEUED
    partition: str = "default"
    file_id: str | None = None
    user_id: int | None = None
    job_metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
