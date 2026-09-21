"""Helpers for presenting safe, compact task failure summaries."""

from __future__ import annotations

_ERROR_SUMMARY_MAX_LENGTH = 500


def summarize_task_error(error: str | None) -> str | None:
    """Return a compact failure reason suitable for admin list views."""
    if not error:
        return None

    for line in reversed(error.splitlines()):
        summary = " ".join(line.split())
        if not summary or summary.startswith("Traceback"):
            continue
        if len(summary) > _ERROR_SUMMARY_MAX_LENGTH:
            return f"{summary[: _ERROR_SUMMARY_MAX_LENGTH - 3].rstrip()}..."
        return summary
    return None
