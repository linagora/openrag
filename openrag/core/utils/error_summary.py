"""Helpers for presenting safe, compact task failure summaries."""

from __future__ import annotations

import re

_ERROR_SUMMARY_MAX_LENGTH = 500
_EXCEPTION_HEADER = re.compile(r"^(?:[\w.]+\.)?(?P<type>[A-Z]\w*)(?::\s*(?P<message>.*))?$")
_CHAIN_SEPARATORS = (
    "During handling of the above exception",
    "The above exception was the direct cause",
)


def failure_reason_from_exception(exc: BaseException) -> str:
    """Capture the exception type and first meaningful message line."""
    exception_type = type(exc).__name__
    message = next((" ".join(line.split()) for line in str(exc).splitlines() if line.strip()), "")
    return f"{exception_type}: {message}" if message else exception_type


def extract_task_error_reason(error: str | None) -> str | None:
    """Recover an exception header from a legacy stored traceback."""
    if not error:
        return None

    candidates: list[str] = []
    meaningful_lines: list[str] = []
    for raw_line in error.splitlines():
        line = raw_line.strip().lstrip("|+").strip()
        if line.startswith(_CHAIN_SEPARATORS):
            candidates.clear()
            continue
        if line and not line.startswith("Traceback"):
            meaningful_lines.append(" ".join(line.split()))
        match = _EXCEPTION_HEADER.match(line)
        if match:
            message = " ".join((match.group("message") or "").split())
            exception_type = match.group("type")
            candidates.append(f"{exception_type}: {message}" if message else exception_type)
    if candidates:
        return candidates[0]
    return meaningful_lines[0] if len(meaningful_lines) == 1 else None


def summarize_task_error(error: str | None, *, reason: str | None = None) -> str | None:
    """Return a compact failure reason suitable for admin list views."""
    value = " ".join((reason or extract_task_error_reason(error) or "").split())
    if not value:
        return None
    if len(value) <= _ERROR_SUMMARY_MAX_LENGTH:
        return value
    return f"{value[: _ERROR_SUMMARY_MAX_LENGTH - 3].rstrip()}..."
