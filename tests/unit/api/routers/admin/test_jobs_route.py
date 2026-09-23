from __future__ import annotations

import json

import pytest
from api.routers.admin.indexing import get_task_error
from api.routers.admin.jobs import list_tasks


class _Request:
    def url_for(self, name: str, **kwargs) -> str:
        return f"https://openrag.test/{name}/{kwargs['task_id']}"


class _JobService:
    async def list_tasks(self, **_kwargs):
        return [
            {
                "task_id": "task-1",
                "state": "COMPLETED",
                "outcome": "completed_degraded",
                "details": {"degraded_stages": ["caption"]},
                "created_at": "2026-09-17T10:00:00+00:00",
                "duration_ms": 1200,
            }
        ]


class _FailedJobService:
    async def list_tasks(self, **_kwargs):
        return [
            {
                "task_id": "task-failed",
                "state": "FAILED",
                "outcome": "failed",
                "details": {},
                "created_at": None,
                "duration_ms": 1200,
                "error_summary": "ValueError: parser failed",
            }
        ]


class _IndexingService:
    def __init__(self, *, error: str | None = None, error_reason: str | None = None) -> None:
        self.error = error or "Traceback (most recent call last):\nValueError:   parser   failed"
        self.error_reason = error_reason or "ValueError: parser failed"
        self.reason_calls = 0

    async def get_task_error(self, _task_id: str) -> str:
        return self.error

    async def get_task_error_reason(self, _task_id: str) -> str:
        self.reason_calls += 1
        return self.error_reason


@pytest.mark.asyncio
async def test_jobs_route_exposes_the_degraded_outcome() -> None:
    response = await list_tasks(
        _Request(),
        user={"id": 7, "is_admin": False},
        service=_JobService(),
    )

    payload = json.loads(response.body)
    assert payload["tasks"][0]["outcome"] == "completed_degraded"
    assert payload["tasks"][0]["details"]["degraded_stages"] == ["caption"]


@pytest.mark.asyncio
async def test_jobs_route_exposes_failure_summary_supplied_for_an_admin() -> None:
    response = await list_tasks(
        _Request(),
        user={"id": 7, "is_admin": True},
        service=_FailedJobService(),
    )

    payload = json.loads(response.body)
    assert payload["tasks"][0]["error_summary"] == "ValueError: parser failed"


@pytest.mark.asyncio
async def test_task_error_uses_the_same_normalized_summary_as_the_list() -> None:
    payload = await get_task_error(
        "task-failed",
        task_details={},
        service=_IndexingService(),
        user={"id": 7, "is_admin": True},
    )

    assert payload == {
        "task_id": "task-failed",
        "reason": "ValueError: parser failed",
        "summary": "ValueError: parser failed",
        "traceback": [
            "Traceback (most recent call last):",
            "ValueError:   parser   failed",
        ],
    }


@pytest.mark.asyncio
async def test_admin_task_error_returns_the_uncapped_reason() -> None:
    reason = "RuntimeError: " + "x" * 700
    service = _IndexingService(error="RuntimeError: fallback", error_reason=reason)

    payload = await get_task_error(
        "task-failed",
        task_details={},
        service=service,
        user={"id": 7, "is_admin": True},
    )

    assert payload["reason"] == reason
    assert len(payload["reason"]) > 500


@pytest.mark.asyncio
async def test_task_owner_does_not_receive_or_fetch_the_internal_reason() -> None:
    service = _IndexingService(error="internal traceback", error_reason="RuntimeError: secret")

    payload = await get_task_error(
        "task-failed",
        task_details={},
        service=service,
        user={"id": 7, "is_admin": False},
    )

    assert "reason" not in payload
    assert payload["summary"] == "Task failed. Contact an administrator for details."
    assert service.reason_calls == 0
