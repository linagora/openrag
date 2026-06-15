"""Admin schemas for Phase 14 partition preset assignment."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


def _normalize_name(value: str) -> str:
    """Trim a partition/preset reference and reject blank values."""
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


def _reject_explicit_null(field_name: str, value):
    """Reject explicit null while still allowing omitted optional fields."""
    if value is None:
        raise ValueError(f"{field_name} cannot be null")
    return value


class CreatePartitionRequest(BaseModel):
    """Request body for creating a partition with preset references."""

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
        """Normalize non-null partition and preset names."""
        return _normalize_name(value)


class UpdatePartitionRequest(BaseModel):
    """Request body for updating partition preset assignments."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    embedder: str | None = None
    indexation_preset: str | None = None
    retrieval_preset: str | None = None
    chat_history_depth: int | None = Field(default=None, ge=0)
    chat_llm: str | None = None

    @field_validator("embedder", "indexation_preset", "retrieval_preset")
    @classmethod
    def validate_non_empty_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Normalize updated references and reject explicit null."""
        return _normalize_name(_reject_explicit_null(info.field_name, value))

    @field_validator("description")
    @classmethod
    def reject_null_description(cls, value: str | None) -> str:
        """Reject explicit null for description updates."""
        return _reject_explicit_null("description", value)

    @field_validator("chat_history_depth")
    @classmethod
    def reject_null_chat_history_depth(cls, value: int | None) -> int:
        """Reject explicit null for chat history depth updates."""
        return _reject_explicit_null("chat_history_depth", value)

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdatePartitionRequest:
        """Reject empty update payloads."""
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class PartitionDetailResponse(BaseModel):
    """Response body for a resolved partition configuration."""

    name: str
    description: str
    embedder: str
    indexation_preset: str
    retrieval_preset: str
    indexation_pipeline: dict[str, Any]
    retrieval_pipeline: dict[str, Any]
    dimension: int
    created_at: datetime
    document_count: int = 0


__all__ = [
    "CreatePartitionRequest",
    "PartitionDetailResponse",
    "UpdatePartitionRequest",
]
