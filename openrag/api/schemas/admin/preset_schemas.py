"""Admin schemas for the Phase 14 pipeline preset registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

PresetType = Literal["indexation", "retrieval"]


def _normalize_name(value: str) -> str:
    """Trim a preset name and reject blank values."""
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


def _reject_explicit_null(field_name: str, value):
    """Reject explicit null while still allowing omitted optional fields."""
    if value is None:
        raise ValueError(f"{field_name} cannot be null")
    return value


class CreatePresetRequest(BaseModel):
    """Request body for creating an indexation or retrieval preset."""

    model_config = ConfigDict(extra="forbid")

    name: str
    preset_type: PresetType
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """Normalize the preset name."""
        return _normalize_name(value)


class UpdatePresetRequest(BaseModel):
    """Request body for updating an existing preset."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    config: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Normalize the optional replacement name and reject null."""
        return _normalize_name(_reject_explicit_null(info.field_name, value))

    @field_validator("config")
    @classmethod
    def validate_config(cls, value: dict[str, Any] | None) -> dict[str, Any]:
        """Reject explicit null for preset config updates."""
        return _reject_explicit_null("config", value)

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdatePresetRequest:
        """Reject empty update payloads."""
        if not self.model_fields_set or not any(getattr(self, field) is not None for field in ("name", "config")):
            raise ValueError("at least one field must be provided")
        return self


class PresetResponse(BaseModel):
    """Response body for a stored pipeline preset."""

    name: str
    preset_type: PresetType
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PresetOptionsResponse(BaseModel):
    """Response body listing allowed preset option values."""

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
