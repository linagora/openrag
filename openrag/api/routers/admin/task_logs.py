# Re-exported for callers/tests that import these from this module; the single
# implementation now lives in core.utils so the api route and the MCP server
# share it.
from core.utils.log_tail import (
    MAX_TASK_LOG_LINES,
    collect_task_logs,
    iter_file_lines_reversed,
)

__all__ = ["MAX_TASK_LOG_LINES", "collect_task_logs", "iter_file_lines_reversed"]
