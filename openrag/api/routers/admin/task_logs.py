import json
from pathlib import Path

MAX_TASK_LOG_LINES = 5000
TASK_LOG_READ_BLOCK_SIZE = 64 * 1024


def iter_file_lines_reversed(path: Path, block_size: int = TASK_LOG_READ_BLOCK_SIZE):
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
