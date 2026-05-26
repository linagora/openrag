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
            if record.get("extra", {}).get("task_id") == task_id:
                logs.append(
                    f"{record['time']['repr']} - {record['level']['name']} - {record['message']} - {(record['extra'])}"
                )
                if len(logs) >= max_lines:
                    break
        except json.JSONDecodeError:
            continue

    return logs[::-1]
