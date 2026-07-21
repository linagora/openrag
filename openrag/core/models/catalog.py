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

# Kept inside TaskInfo.details.metadata so deployments can add lifecycle timing
# without replacing an already-running detached TaskStateManager actor.
TASK_CREATED_AT_METADATA_KEY = "_openrag_job_created_at"
TASK_FINISHED_AT_METADATA_KEY = "_openrag_job_finished_at"


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
    content_sha256: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IndexationJob(BaseModel):
    """An indexation job tracking batch document processing."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = JobStatus.QUEUED
    total_documents: int = 0
    partition: str = "default"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
