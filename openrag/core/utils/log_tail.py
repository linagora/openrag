"""Locate and tail the application log.

The single source of truth for the JSON application-log path (``app.json``
under the configured ``log_dir``), the backwards block reader that collects
the newest matching lines without loading the whole file, and the loguru-record
parser that surfaces one task's lines. Shared by the logger sink, the admin
task-logs route (``api``) and the MCP ``get_task_logs`` tool (``services``) —
the single implementation lives here so no layer duplicates it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

DEFAULT_BLOCK_SIZE = 64 * 1024
MAX_TASK_LOG_LINES = 5000
APP_LOG_FILENAME = "app.json"


def app_log_file(log_dir: str | Path | None) -> Path:
    """Canonical path of the JSON application log under *log_dir*.

    Falls back to ``logs/`` when *log_dir* is unset/empty.
    """
    return Path(log_dir or "logs") / APP_LOG_FILENAME


def iter_file_lines_reversed(path: Path, block_size: int = DEFAULT_BLOCK_SIZE) -> Iterator[str]:
    """Yield the file's lines newest-first, reading from the end in blocks."""
    with path.open("rb") as f:
        f.seek(0, 2)
        position = f.tell()
        pending = b""

        while position > 0:
            read_size = min(block_size, position)
            position -= read_size
            f.seek(position)
            pending = f.read(read_size) + pending
            lines = pending.split(b"\n")
            pending = lines[0]

            for line in reversed(lines[1:]):
                if line:
                    yield line.decode(errors="replace")

        if pending:
            yield pending.decode(errors="replace")


def collect_task_logs(log_file: Path, task_id: str, max_lines: int) -> list[str]:
    """Return chronological loguru log lines tagged with ``task_id``.

    Scans the JSON-per-line application log newest-first (block by block, never
    loading the whole file) until ``max_lines`` matching records are collected,
    then restores chronological order. ``max_lines`` is bounded by
    ``MAX_TASK_LOG_LINES``.
    """
    if max_lines < 1 or max_lines > MAX_TASK_LOG_LINES:
        raise ValueError(f"max_lines must be between 1 and {MAX_TASK_LOG_LINES}")

    logs: list[str] = []
    for line in iter_file_lines_reversed(log_file):
        try:
            record = json.loads(line).get("record", {})
            extra = record.get("extra") or {}
            if extra.get("task_id") != task_id:
                continue
            time_repr = (record.get("time") or {}).get("repr")
            level_name = (record.get("level") or {}).get("name")
            message = record.get("message")
            if not all((time_repr, level_name, message)):
                continue
            logs.append(f"{time_repr} - {level_name} - {message} - {extra}")
            if len(logs) >= max_lines:
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    return logs[::-1]


__all__ = [
    "app_log_file",
    "iter_file_lines_reversed",
    "collect_task_logs",
    "APP_LOG_FILENAME",
    "DEFAULT_BLOCK_SIZE",
    "MAX_TASK_LOG_LINES",
]
