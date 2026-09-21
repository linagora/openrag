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
    async def get_task_error(self, _task_id: str) -> str:
        return "Traceback (most recent call last):\nValueError:   parser   failed"


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
        "summary": "ValueError: parser failed",
        "traceback": [
            "Traceback (most recent call last):",
            "ValueError:   parser   failed",
        ],
    }
