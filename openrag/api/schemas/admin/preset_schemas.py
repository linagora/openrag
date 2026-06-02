"""Admin schemas for the Phase 14 pipeline preset registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PresetType = Literal["indexation", "retrieval"]


def _normalize_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


class CreatePresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    preset_type: PresetType
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_name(value)


class UpdatePresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    config: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdatePresetRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class PresetResponse(BaseModel):
    name: str
    preset_type: PresetType
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PresetOptionsResponse(BaseModel):
    chunking_strategies: list[str]
    retrieval_types: list[str]
    reranker_providers: list[str]


__all__ = [
    "CreatePresetRequest",
    "PresetOptionsResponse",
    "PresetResponse",
    "PresetType",
    "UpdatePresetRequest",
]
