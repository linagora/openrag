from __future__ import annotations

import json

import pytest
from routers.task_logs import collect_task_logs, iter_file_lines_reversed


def _record(task_id: str, message: str) -> str:
    return json.dumps(
        {
            "record": {
                "time": {"repr": "2026-05-26 10:00:00"},
                "level": {"name": "INFO"},
                "message": message,
                "extra": {"task_id": task_id},
            }
        }
    )


def test_task_logs_rejects_non_positive_max_lines(tmp_path):
    log_file = tmp_path / "app.json"
    log_file.write_text(_record("task-1", "indexed") + "\n")

    with pytest.raises(ValueError, match="max_lines must be between"):
        collect_task_logs(log_file, "task-1", max_lines=0)


def test_iter_file_lines_reversed_reads_across_blocks(tmp_path):
    log_file = tmp_path / "app.json"
    log_file.write_text("oldest\nmiddle\nnewest\n")

    assert list(iter_file_lines_reversed(log_file, block_size=7)) == [
        "newest",
        "middle",
        "oldest",
    ]


def test_collect_task_logs_returns_newest_matches_in_chronological_order(tmp_path):
    log_file = tmp_path / "app.json"
    log_file.write_text(
        "\n".join(
            [
                _record("task-1", "old"),
                "not-json",
                _record("other-task", "ignore"),
                _record("task-1", "middle"),
                _record("task-1", "new"),
            ]
        )
        + "\n"
    )

    logs = collect_task_logs(log_file, "task-1", max_lines=2)

    assert "middle" in logs[0]
    assert "new" in logs[1]
