from __future__ import annotations

import json

import pytest
from api.routers.admin.indexing import get_task_error, get_task_stage_progress
from api.routers.admin.jobs import list_tasks
from fastapi import HTTPException


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


class _PaginatedJobService:
    def __init__(self) -> None:
        self.kwargs = None

    async def list_tasks(self, **kwargs):
        self.kwargs = kwargs
        return {
            "tasks": [
                {
                    "task_id": "task-page-2",
                    "state": "SERIALIZING",
                    "outcome": "active",
                    "details": {"partition": "docs"},
                    "created_at": None,
                    "duration_ms": None,
                }
            ],
            "pagination": {
                "offset": 10,
                "limit": 10,
                "total": 21,
                "has_more": True,
                "available_partitions": ["archive", "docs"],
            },
        }


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


class _StageJobService:
    def __init__(self, result=None) -> None:
        self.result = result or {"events": [], "history_quality": "legacy", "has_more": False}
        self.call = None

    async def get_task_stage_progress(self, task_id, *, before, limit):
        self.call = (task_id, before, limit)
        return self.result


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
async def test_jobs_route_preserves_server_pagination_metadata() -> None:
    service = _PaginatedJobService()

    response = await list_tasks(
        _Request(),
        task_status="active",
        limit=10,
        offset=10,
        user={"id": 7, "is_admin": False},
        service=service,
    )

    payload = json.loads(response.body)
    assert service.kwargs["limit"] == 10
    assert service.kwargs["offset"] == 10
    assert payload["pagination"] == {
        "offset": 10,
        "limit": 10,
        "total": 21,
        "has_more": True,
        "available_partitions": ["archive", "docs"],
    }
    assert payload["tasks"][0]["task_id"] == "task-page-2"


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


@pytest.mark.asyncio
async def test_stage_history_route_passes_a_bounded_cursor_to_the_job_service() -> None:
    service = _StageJobService({"events": [{"stage_id": "parse"}], "history_quality": "complete", "has_more": False})

    response = await get_task_stage_progress(
        "task-1",
        before_attempt=2,
        before_sequence=8,
        limit=25,
        task_details={"user_id": 7},
        job_service=service,
    )

    payload = json.loads(response.body)
    assert payload["history_quality"] == "complete"
    assert service.call[0] == "task-1"
    assert service.call[1].attempt == 2
    assert service.call[1].sequence == 8
    assert service.call[2] == 25


@pytest.mark.asyncio
async def test_stage_history_route_rejects_a_partial_cursor() -> None:
    with pytest.raises(HTTPException, match="must be supplied together") as exc_info:
        await get_task_stage_progress(
            "task-1",
            before_attempt=2,
            before_sequence=None,
            limit=25,
            task_details={"user_id": 7},
            job_service=_StageJobService(),
        )
    assert exc_info.value.status_code == 400
