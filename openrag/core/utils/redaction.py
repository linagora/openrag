"""Helpers for shaping public data without exposing secrets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REDACTED_SECRET = "<redacted>"

SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_token",
        "auth_token",
        "chainlit_auth_secret",
        "client_secret",
        "hf_token",
        "oidc_client_secret",
        "oidc_token_encryption_key",
        "password",
        "secret",
        "secret_key",
        "token",
        "token_encryption_key",
    }
)


def is_secret_field(key: str) -> bool:
    """Return true only for known secret field names, not fuzzy token matches."""
    return key.lower() in SECRET_FIELD_NAMES


def redact_secrets(value: Any) -> Any:
    """Recursively redact values for known secret fields without mutating input."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED_SECRET if is_secret_field(str(key)) else redact_secrets(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


def redact_secret_mapping(extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop secret fields from endpoint extras while preserving non-secret metadata."""
    return {key: redact_secrets(item) for key, item in dict(extra or {}).items() if not is_secret_field(str(key))}


def preserve_existing_secrets(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Keep stored secrets when an update payload omits or echoes a redacted value."""
    return _preserve_existing_secrets(existing or {}, incoming)


def _preserve_existing_secrets(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(incoming)
    for key, value in existing.items():
        incoming_value = merged.get(key)
        if is_secret_field(str(key)):
            if key not in merged or incoming_value == REDACTED_SECRET:
                merged[key] = value
            continue
        if isinstance(value, Mapping) and isinstance(incoming_value, Mapping):
            merged[key] = _preserve_existing_secrets(value, incoming_value)
        elif isinstance(value, list) and isinstance(incoming_value, list):
            merged[key] = _preserve_existing_secret_lists(value, incoming_value)
    return merged


def _preserve_existing_secret_lists(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = list(incoming)
    for index, existing_item in enumerate(existing):
        if index >= len(merged):
            break
        incoming_item = merged[index]
        if isinstance(existing_item, Mapping) and isinstance(incoming_item, Mapping):
            merged[index] = _preserve_existing_secrets(existing_item, incoming_item)
        elif isinstance(existing_item, list) and isinstance(incoming_item, list):
            merged[index] = _preserve_existing_secret_lists(existing_item, incoming_item)
    return merged


__all__ = [
    "REDACTED_SECRET",
    "SECRET_FIELD_NAMES",
    "is_secret_field",
    "preserve_existing_secrets",
    "redact_secret_mapping",
    "redact_secrets",
]
