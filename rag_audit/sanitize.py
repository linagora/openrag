from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

SENSITIVE_KEYS = {"sample", "secret", "token", "api_key", "password", "credentials"}

SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|api[_-]?key)[_-]?[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"(?i)(password|secret|token|credentials?)\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]


def sanitize_audit_result(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_audit_result(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_audit_result(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    lower = key.lower()
    return any(sensitive in lower for sensitive in SENSITIVE_KEYS)


def _sanitize_string(value: str) -> str:
    sanitized = value
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(REDACTED, sanitized)
    return sanitized
