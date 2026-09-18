from __future__ import annotations

import json

import pytest
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
