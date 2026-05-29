import json
from pathlib import Path

# Re-exported for callers/tests that import it from this module; the single
# implementation now lives in core.utils so the MCP server can share it.
from core.utils.log_tail import iter_file_lines_reversed

MAX_TASK_LOG_LINES = 5000


def collect_task_logs(log_file: Path, task_id: str, max_lines: int) -> list[str]:
    if max_lines < 1 or max_lines > MAX_TASK_LOG_LINES:
        raise ValueError(f"max_lines must be between 1 and {MAX_TASK_LOG_LINES}")

    logs = []
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
