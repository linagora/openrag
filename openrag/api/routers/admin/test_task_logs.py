from __future__ import annotations

import json

from api.routers.admin.task_logs import collect_task_logs


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


def test_collect_task_logs_skips_malformed_records(tmp_path):
    log_file = tmp_path / "app.json"
    log_file.write_text(
        "\n".join(
            [
                _record("task-1", "old"),
                json.dumps({"record": {"extra": {"task_id": "task-1"}}}),
                json.dumps({"record": []}),
                "not-json",
                _record("task-1", "new"),
            ]
        )
        + "\n"
    )

    logs = collect_task_logs(log_file, "task-1", max_lines=10)

    assert len(logs) == 2
    assert "old" in logs[0]
    assert "new" in logs[1]
