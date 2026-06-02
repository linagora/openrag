"""Admin schemas for the Phase 14 model endpoint registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelEndpointType = Literal["embedder", "reranker", "llm", "vlm"]


def _normalize_name(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


class CreateModelEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model_type: ModelEndpointType
    endpoint: str
    model_name: str | None = None
    batch_size: int = Field(default=32, gt=0)
    timeout: float = Field(default=30.0, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_name(value)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("endpoint must be non-empty")
        return value.rstrip("/")


class UpdateModelEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    endpoint: str | None = None
    model_name: str | None = None
    batch_size: int | None = Field(default=None, gt=0)
    timeout: float | None = Field(default=None, gt=0)
    extra: dict[str, Any] | None = None
    is_default: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return _normalize_name(value) if value is not None else None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("endpoint must be non-empty")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdateModelEndpointRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class ModelEndpointResponse(BaseModel):
    name: str
    model_type: ModelEndpointType
    endpoint: str
    model_name: str | None
    batch_size: int
    timeout: float
    extra: dict[str, Any]
    is_default: bool
    created_at: datetime
    updated_at: datetime


class ValidateEndpointResponse(BaseModel):
    reachable: bool
    model_found: bool | None = None
    models_served: list[str] | None = None
    detail: str | None = None


__all__ = [
    "CreateModelEndpointRequest",
    "ModelEndpointResponse",
    "ModelEndpointType",
    "UpdateModelEndpointRequest",
    "ValidateEndpointResponse",
]
