"""Helpers for shaping public data without exposing secrets."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REDACTED_SECRET = "<redacted>"
MASK_SUFFIX = "********"
MASK_PREFIX_LENGTH = 3
MIN_PREFIX_MASK_LENGTH = 8

SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_token",
        "access_key",
        "auth_token",
        "chainlit_auth_secret",
        "client_secret",
        "hf_token",
        "oidc_client_secret",
        "oidc_token_encryption_key",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "signing_key",
        "token",
        "token_encryption_key",
    }
)
SECRET_FIELD_SUFFIXES = frozenset(
    {
        "_access_key",
        "_api_key",
        "_auth_token",
        "_password",
        "_private_key",
        "_refresh_token",
        "_secret",
        "_signing_key",
        "_token",
        "_token_encryption_key",
    }
)


def is_secret_field(key: str) -> bool:
    """Return true only for known secret field names, not fuzzy token matches."""
    normalized = key.lower()
    return normalized in SECRET_FIELD_NAMES or any(normalized.endswith(suffix) for suffix in SECRET_FIELD_SUFFIXES)


def mask_secret_value(value: Any) -> str:
    """Return a public masked value with a short prefix when that is useful."""
    if not isinstance(value, str) or len(value) < MIN_PREFIX_MASK_LENGTH:
        return REDACTED_SECRET
    return f"{value[:MASK_PREFIX_LENGTH]}{MASK_SUFFIX}"


def is_masked_secret_value(value: Any) -> bool:
    """Return true for placeholders produced by the public redaction layer."""
    return (
        isinstance(value, str) and len(value) == MASK_PREFIX_LENGTH + len(MASK_SUFFIX) and value.endswith(MASK_SUFFIX)
    )


def is_clear_secret_value(value: Any) -> bool:
    """Return true when an update explicitly clears a stored secret."""
    return value is None or value == ""


def is_secret_placeholder_value(value: Any) -> bool:
    """Return true for redacted values that should never be stored as secrets."""
    return value == REDACTED_SECRET or is_masked_secret_value(value)


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
    """Return the public model-endpoint extra shape shown in Admin UI."""
    return _redact_endpoint_extra(dict(extra or {}))


def _redact_endpoint_extra(value: Any, key: str | None = None) -> Any:
    if key is not None and is_secret_field(key):
        return mask_secret_value(value)
    if isinstance(value, Mapping):
        return {entry_key: _redact_endpoint_extra(item, str(entry_key)) for entry_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_endpoint_extra(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_endpoint_extra(item) for item in value)
    return value


def preserve_existing_secrets(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Keep stored secrets when an update payload omits or echoes a redacted value."""
    return _preserve_existing_secrets(existing or {}, incoming)


def _preserve_existing_secrets(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(incoming)
    for key, value in existing.items():
        incoming_value = merged.get(key)
        if is_secret_field(str(key)):
            if key in merged and is_clear_secret_value(incoming_value):
                merged.pop(key, None)
                continue
            if key not in merged or is_secret_placeholder_value(incoming_value):
                merged[key] = value
            continue
        if isinstance(value, Mapping) and isinstance(incoming_value, Mapping):
            merged[key] = _preserve_existing_secrets(value, incoming_value)
        elif isinstance(value, list) and isinstance(incoming_value, list):
            merged[key] = _preserve_existing_secret_lists(value, incoming_value)
    for key, value in list(merged.items()):
        if is_secret_field(str(key)) and (is_clear_secret_value(value) or is_secret_placeholder_value(value)):
            merged.pop(key, None)
    return merged


def _preserve_existing_secret_lists(existing: list[Any], incoming: list[Any]) -> list[Any]:
    if len(existing) == 1 and len(incoming) == 1:
        return [_preserve_existing_list_item(existing[0], incoming[0])]

    remaining_existing = list(existing)
    merged: list[Any] = []
    for incoming_item in incoming:
        match_index = _find_matching_secret_list_item(remaining_existing, incoming_item)
        if match_index is None:
            merged.append(_drop_unbacked_secret_placeholders(incoming_item))
            continue
        existing_item = remaining_existing.pop(match_index)
        merged.append(_preserve_existing_list_item(existing_item, incoming_item))
    return merged


def _preserve_existing_list_item(existing_item: Any, incoming_item: Any) -> Any:
    if isinstance(existing_item, Mapping) and isinstance(incoming_item, Mapping):
        return _preserve_existing_secrets(existing_item, incoming_item)
    if isinstance(existing_item, list) and isinstance(incoming_item, list):
        return _preserve_existing_secret_lists(existing_item, incoming_item)
    return incoming_item


def _find_matching_secret_list_item(existing: list[Any], incoming_item: Any) -> int | None:
    incoming_identity = _non_secret_identity(incoming_item)
    if not _has_non_secret_identity(incoming_identity):
        return None
    matches = [
        index
        for index, existing_item in enumerate(existing)
        if _non_secret_identity(existing_item) == incoming_identity
    ]
    return matches[0] if len(matches) == 1 else None


def _non_secret_identity(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _non_secret_identity(item) for key, item in value.items() if not is_secret_field(str(key))}
    if isinstance(value, list):
        return [_non_secret_identity(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_non_secret_identity(item) for item in value)
    return value


def _has_non_secret_identity(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_has_non_secret_identity(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_non_secret_identity(item) for item in value)
    return True


def _drop_unbacked_secret_placeholders(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if is_secret_field(str(key)) and (is_clear_secret_value(item) or is_secret_placeholder_value(item)):
                continue
            cleaned[key] = _drop_unbacked_secret_placeholders(item)
        return cleaned
    if isinstance(value, list):
        return [_drop_unbacked_secret_placeholders(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_drop_unbacked_secret_placeholders(item) for item in value)
    return value


__all__ = [
    "REDACTED_SECRET",
    "SECRET_FIELD_NAMES",
    "is_masked_secret_value",
    "is_secret_field",
    "is_clear_secret_value",
    "is_secret_placeholder_value",
    "mask_secret_value",
    "preserve_existing_secrets",
    "redact_secret_mapping",
    "redact_secrets",
]
