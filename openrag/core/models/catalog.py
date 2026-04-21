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
    status: DocumentStatus = DocumentStatus.QUEUED
    error_message: str | None = None
    created_by: int | None = None
    relationship_id: str | None = None
    parent_id: str | None = None
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
