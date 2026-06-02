"""Admin schemas for Phase 14 partition preset assignment."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _normalize_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


class CreatePartitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    embedder: str = "default"
    indexation_preset: str = "default"
    retrieval_preset: str = "default"
    chat_history_depth: int = Field(default=0, ge=0)
    chat_llm: str | None = None

    @field_validator("name", "embedder", "indexation_preset", "retrieval_preset")
    @classmethod
    def validate_non_empty_name(cls, value: str) -> str:
        return _normalize_name(value)


class UpdatePartitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    embedder: str | None = None
    indexation_preset: str | None = None
    retrieval_preset: str | None = None
    chat_history_depth: int | None = Field(default=None, ge=0)
    chat_llm: str | None = None

    @field_validator("embedder", "indexation_preset", "retrieval_preset")
    @classmethod
    def validate_non_empty_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdatePartitionRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class PartitionDetailResponse(BaseModel):
    name: str
    description: str
    embedder: str
    indexation_preset: str
    retrieval_preset: str
    indexation_pipeline: dict[str, Any]
    retrieval_pipeline: dict[str, Any]
    dimension: int
    created_at: datetime


__all__ = [
    "CreatePartitionRequest",
    "PartitionDetailResponse",
    "UpdatePartitionRequest",
]
