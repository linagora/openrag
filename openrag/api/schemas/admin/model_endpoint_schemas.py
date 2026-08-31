"""Admin schemas for the Phase 14 model endpoint registry."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from core.config.model_endpoints import (
    LLM_CONTEXT_SIZE_KEY,
    LLM_OUTPUT_TOKENS_KEY,
    MOSS_TRANSCRIPT_OUTPUT_FORMATS,
    STT_LANGUAGE_KEY,
    STT_TRANSCRIPT_OUTPUT_FORMAT_KEY,
)
from core.utils.redaction import redact_secret_mapping
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

ModelEndpointType = Literal["embedder", "reranker", "llm", "vlm", "stt"]

# LLM token budgets that the admin UI edits as first-class fields but stores in
# ``extra`` — validated here so a typo can't persist a nonsensical value.
_LLM_TOKEN_EXTRA_KEYS = (LLM_CONTEXT_SIZE_KEY, LLM_OUTPUT_TOKENS_KEY)

# Allowlist, not a denylist: `name` is a single path segment in every
# single-endpoint route (see `_normalize_name`), and enumerating unsafe values
# one at a time as they're discovered — first `/` (#768), then the RFC 3986
# dot-segments `.`/`..` — never closes the class. Anchoring both ends on
# alphanumeric rules out `/`, `.`, `..`, and any leading/trailing separator by
# construction, while `.`/`_`/`-` stay available in the middle for realistic
# names like `gpt-4.1` or `jina_v3`.
_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_NAME_MAX_LENGTH = 128


def _normalize_name(value: str) -> str:
    """Trim a user-facing registry name and reject any value unsafe as a URL path segment.

    ``name`` is embedded as a single path segment in every single-endpoint route
    (``GET/PUT/DELETE /model-endpoints/{model_type}/{name}``, ``.../set-default``,
    ``.../reveal-api-key``, ``.../validate``). A value outside ``_NAME_PATTERN``
    — a ``/`` (splits across path segments), the exact values ``.``/``..``
    (RFC 3986 dot-segments: browsers and HTTP clients normalize these out of
    the URL before the request is even sent, resolving to the collection route
    or dropping the ``model_type`` segment entirely), or anything else that
    doesn't start/end alphanumeric — would leave the row visible in the list
    endpoint but permanently unreachable by get/update/delete/set-default,
    surfacing as a spurious "not found". Percent-encoding never helps: ASGI
    servers decode ``%2F``/dot-segment escapes before Starlette's router sees
    the path.
    """
    value = value.strip()
    if not value:
        raise ValueError("name must be non-empty")
    if len(value) > _NAME_MAX_LENGTH:
        raise ValueError(f"name must be at most {_NAME_MAX_LENGTH} characters")
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(
            "name must start and end with a letter or digit, and contain only "
            "letters, digits, '.', '_', or '-' (it is used as a URL path segment)"
        )
    return value


def _normalize_endpoint(value: str) -> str:
    """Trim an endpoint URL and reject values that normalize to empty."""
    normalized = value.strip().rstrip("/")
    if not normalized:
        raise ValueError("endpoint must be non-empty")
    return normalized


def validate_llm_token_extra(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reject non-positive-int LLM token budgets carried in ``extra``.

    ``bool`` is an ``int`` subclass in Python, so it is excluded explicitly —
    ``true`` must not slip through as ``1``.

    Only ever applied to **LLM** endpoints. These two keys are meaningful only
    there, so enforcing them globally would reserve the names across every
    endpoint type and reject an embedder / reranker / VLM carrying same-named
    provider metadata of a different shape. The create schema scopes the call
    by reading its own ``model_type``; the update route scopes it from the path
    parameter (``UpdateModelEndpointRequest`` has no ``model_type`` field).

    Raises ``ValueError`` so a pydantic ``field_validator`` can surface it as a
    normal 422; callers outside pydantic translate it themselves.
    """
    if not extra:
        return extra
    for key in _LLM_TOKEN_EXTRA_KEYS:
        if key not in extra:
            continue
        value = extra[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"extra.{key} must be a positive integer")
    return extra


def validate_stt_fields(model_name: str | None, extra: dict[str, Any] | None) -> None:
    """Validate fields that are meaningful for an OpenAI-compatible STT endpoint.

    ``model`` is required by ``/audio/transcriptions``. A language hint is
    intentionally permissive: providers accept either ISO 639-1 values such as
    ``fr`` or broader BCP-47 tags, so the API only requires a non-empty string.
    The optional response-format control accepts the supported MOSS-specific
    values only.
    """
    if not model_name or not model_name.strip():
        raise ValueError("model_name is required for an STT endpoint")
    if extra is not None and STT_LANGUAGE_KEY in extra:
        language = extra[STT_LANGUAGE_KEY]
        if not isinstance(language, str) or not language.strip():
            raise ValueError(f"extra.{STT_LANGUAGE_KEY} must be a non-empty language code")

    if extra is None or STT_TRANSCRIPT_OUTPUT_FORMAT_KEY not in extra:
        return
    output_format = extra[STT_TRANSCRIPT_OUTPUT_FORMAT_KEY]
    if not isinstance(output_format, str) or output_format not in MOSS_TRANSCRIPT_OUTPUT_FORMATS:
        raise ValueError(
            f"extra.{STT_TRANSCRIPT_OUTPUT_FORMAT_KEY} must be one of {sorted(MOSS_TRANSCRIPT_OUTPUT_FORMATS)!r}"
        )


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

    @field_validator("extra")
    @classmethod
    def validate_extra_token_budgets(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        """Reject non-positive-int LLM token budgets in ``extra`` — LLMs only.

        ``model_type`` is declared before ``extra``, so it is already validated
        and present in ``info.data`` here (absent only when it failed its own
        validation, in which case the request is rejected regardless).
        """
        if info.data.get("model_type") != "llm":
            return value
        return validate_llm_token_extra(value)

    @model_validator(mode="after")
    def validate_stt_endpoint(self) -> CreateModelEndpointRequest:
        if self.model_type == "stt":
            validate_stt_fields(self.model_name, self.extra)
        return self


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

    # NOTE: no ``extra`` token-budget validator here on purpose. This schema
    # carries no ``model_type`` (it is a path parameter), so it cannot tell an
    # LLM update from an embedder/reranker/VLM one, and validating
    # unconditionally would reserve the budget key names across every endpoint
    # type. The update route applies the check once it has the path's
    # ``model_type`` — see ``_reject_non_llm_token_budgets``.

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
    has_api_key: bool = False
    is_default: bool
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def redact_secret_extra(cls, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            data = value.model_dump()
        elif isinstance(value, dict):
            data = dict(value)
        else:
            data = dict(value)
        extra = dict(data.get("extra") or {})
        data["has_api_key"] = bool(extra.get("api_key"))
        data["extra"] = redact_secret_mapping(extra)
        return data


class ValidateEndpointRequest(BaseModel):
    """Request body to validate endpoint values before they are saved (draft)."""

    endpoint: str
    model_type: ModelEndpointType | None = None
    model_name: str | None = None
    api_key: str | None = None
    stored_api_key_model_type: ModelEndpointType | None = None
    stored_api_key_name: str | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        """Normalize the draft endpoint URL before probing it."""
        return _normalize_endpoint(value)

    @field_validator("stored_api_key_name")
    @classmethod
    def validate_stored_api_key_name(cls, value: str | None) -> str | None:
        """Normalize the optional saved endpoint name used as credential source."""
        return _normalize_name(value) if value is not None else None

    @model_validator(mode="after")
    def require_complete_stored_api_key_source(self) -> ValidateEndpointRequest:
        """Require both fields when draft validation reuses a stored key."""
        has_type = self.stored_api_key_model_type is not None
        has_name = self.stored_api_key_name is not None
        if has_type != has_name:
            raise ValueError("stored_api_key_model_type and stored_api_key_name must be provided together")
        return self


class ValidateEndpointResponse(BaseModel):
    """Response body for a model endpoint validation probe."""

    reachable: bool
    model_found: bool | None = None
    models_served: list[str] | None = None
    transcription_supported: bool | None = None
    detail: str | None = None


class RevealApiKeyResponse(BaseModel):
    """Response body for explicitly revealing a stored endpoint API key."""

    api_key: str | None = None


__all__ = [
    "CreateModelEndpointRequest",
    "ModelEndpointResponse",
    "ModelEndpointType",
    "RevealApiKeyResponse",
    "UpdateModelEndpointRequest",
    "ValidateEndpointRequest",
    "ValidateEndpointResponse",
    "validate_stt_fields",
]
