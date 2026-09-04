"""Admin schemas for the DB prompt library."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

# The managed prompt types (mirrors core.models.prompt.PromptType). Declaring
# them as a Literal makes FastAPI reject unknown types at the transport edge
# (422) and renders the enum in the OpenAPI schema.
PromptTypeName = Literal[
    "sys_prompt",
    "query_contextualizer",
    "chunk_contextualizer",
    "image_captioning",
    "hyde",
    "multi_query",
    "spoken_style_answer",
    "topic_tagger",
    "asr_transcription",
]


def _require_non_empty(field_name: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    return value


class CreatePromptRequest(BaseModel):
    """Request body for adding a prompt to the library."""

    model_config = ConfigDict(extra="forbid")

    prompt_type: PromptTypeName
    name: str
    content: str
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def validate_non_empty(cls, value: str, info: ValidationInfo) -> str:
        return _require_non_empty(info.field_name, value)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_content_for_type(self) -> CreatePromptRequest:
        if self.prompt_type != "asr_transcription":
            self.content = _require_non_empty("content", self.content)
        return self


class UpdatePromptRequest(BaseModel):
    """Request body for editing a prompt and/or promoting it to default."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    content: str | None = None
    is_default: bool | None = None

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        # The service validates non-empty content after it knows the stored
        # prompt type. ASR transcription alone may be deliberately blank to
        # select the model's built-in prompt.
        return value.strip()

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return _require_non_empty(info.field_name, value)

    @model_validator(mode="after")
    def require_at_least_one_update(self) -> UpdatePromptRequest:
        if not any(getattr(self, f) is not None for f in ("name", "content", "is_default")):
            raise ValueError("at least one field must be provided")
        return self


class PromptResponse(BaseModel):
    """A stored library prompt."""

    id: str
    prompt_type: str
    name: str
    content: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
    # Number of partitions/presets referencing this prompt by name. Populated by
    # the list endpoint; 0 on single-item responses where usage isn't computed.
    used_by: int = 0


__all__ = [
    "CreatePromptRequest",
    "PromptResponse",
    "PromptTypeName",
    "UpdatePromptRequest",
]
