from typing import Any

from pydantic import BaseModel


class MessageResponse(BaseModel):
    message: str


class TaskStatusResponse(BaseModel):
    task_status_url: str


class DocumentsResponse(BaseModel):
    documents: list[dict[str, Any]]


class FilesResponse(BaseModel):
    files: list[dict[str, Any]] | list[str]
