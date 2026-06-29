"""Admin schemas for the Phase 14 model endpoint registry."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ModelEndpointType = Literal["embedder", "reranker", "llm", "vlm"]


def _normalize_name(value: str) -> str:
    """Trim a user-facing registry name and reject blank values."""
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    return value


def _normalize_endpoint(value: str) -> str:
    """Trim an endpoint URL and reject values that normalize to empty."""
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("endpoint must be non-empty")
    return normalized


class CreateModelEndpointRequest(BaseModel):
    """Request body for registering a model endpoint."""

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
        """Normalize the endpoint registry name."""
        return _normalize_name(value)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Normalize the endpoint URL."""
        return _normalize_endpoint(value)


class UpdateModelEndpointRequest(BaseModel):
    """Request body for updating a registered model endpoint."""

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
        """Normalize the optional replacement name."""
        return _normalize_name(value) if value is not None else None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        """Normalize the optional replacement endpoint URL."""
        if value is None:
            return None
        return _normalize_endpoint(value)

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdateModelEndpointRequest:
        """Reject empty update payloads."""
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class ModelEndpointResponse(BaseModel):
    """Response body for a registered model endpoint."""

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


class ValidateEndpointRequest(BaseModel):
    """Request body to validate endpoint values before they are saved (draft)."""

    endpoint: str
    model_name: str | None = None
    api_key: str | None = None


class ValidateEndpointResponse(BaseModel):
    """Response body for a model endpoint validation probe."""

    reachable: bool
    model_found: bool | None = None
    models_served: list[str] | None = None
    detail: str | None = None


__all__ = [
    "CreateModelEndpointRequest",
    "ModelEndpointResponse",
    "ModelEndpointType",
    "UpdateModelEndpointRequest",
    "ValidateEndpointRequest",
    "ValidateEndpointResponse",
]
