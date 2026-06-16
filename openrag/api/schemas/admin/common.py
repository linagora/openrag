from typing import Any, Literal

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    message: str


class TaskStatusResponse(BaseModel):
    task_status_url: str


class BatchUploadItem(BaseModel):
    """One file entry in a batch upload request."""

    file_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace_ids: list[str] | None = None


class BatchUploadResult(BaseModel):
    """Per-file outcome returned by the batch upload endpoint."""

    file_id: str
    status: Literal["accepted", "failed"]
    task_status_url: str | None = None
    detail: str | None = None


class BatchUploadResponse(BaseModel):
    """Batch upload response with one result per requested file."""

    accepted: int
    failed: int
    results: list[BatchUploadResult]


class DocumentsResponse(BaseModel):
    documents: list[dict[str, Any]]


class FilesResponse(BaseModel):
    files: list[dict[str, Any]] | list[str]
