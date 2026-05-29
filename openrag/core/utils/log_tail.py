"""Read a text file backwards in fixed-size blocks.

Lets callers collect the newest matching lines from a large append-only log
without loading the whole file into memory. Shared by the admin task-logs
route and the MCP ``get_task_logs`` tool.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

DEFAULT_BLOCK_SIZE = 64 * 1024


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


__all__ = ["iter_file_lines_reversed", "DEFAULT_BLOCK_SIZE"]
