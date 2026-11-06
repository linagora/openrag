from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.datastructures import URL
from api.routers.admin.indexing import get_task_error, get_task_status
from api.routers.admin.jobs import list_tasks


class _Request:
    def __init__(self, scheme: str = "https") -> None:
        self.scheme = scheme

    def url_for(self, name: str, **kwargs) -> str:
        path = f"/indexer/task/{kwargs['task_id']}"
        if name == "get_task_error":
            path += "/error"
        return URL(f"{self.scheme}://openrag.test{path}")


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


class _TaskLinksService:
    async def list_tasks(self, **_kwargs):
        return [
            {
                "task_id": "task-completed",
                "state": "COMPLETED",
                "outcome": "completed",
                "details": {},
                "created_at": "2026-09-17T10:00:00+00:00",
                "duration_ms": 1200,
            },
            {
                "task_id": "task-failed",
                "state": "FAILED",
                "outcome": "failed",
                "details": {},
                "created_at": "2026-09-17T10:00:00+00:00",
                "duration_ms": 1200,
                "error_summary": "ValueError: parser failed",
            },
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

class _TaskStatusService:
    async def get_task_state(self, _task_id: str) -> str:
        return "FAILED"
    
@pytest.mark.asyncio
async def test_jobs_route_exposes_the_degraded_outcome() -> None:
    response = await list_tasks(
        _Request(),
        config=SimpleNamespace(server=SimpleNamespace(preferred_url_scheme=None)),
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
        config=SimpleNamespace(server=SimpleNamespace(preferred_url_scheme=None)),
        user={"id": 7, "is_admin": True},
        service=_FailedJobService(),
    )

    payload = json.loads(response.body)
    assert payload["tasks"][0]["error_summary"] == "ValueError: parser failed"


@pytest.mark.parametrize(
    ("preferred_url_scheme", "request_scheme", "expected_scheme"),
    [
        ("https", "http", "https"),
        (None, "http", "http"),
    ],
)
@pytest.mark.asyncio
async def test_jobs_route_builds_task_links_using_configured_or_request_scheme(
    preferred_url_scheme: str | None,
    request_scheme: str,
    expected_scheme: str,
) -> None:
    response = await list_tasks(
        _Request(scheme=request_scheme),
        config=SimpleNamespace(server=SimpleNamespace(preferred_url_scheme=preferred_url_scheme)),
        user={"id": 7, "is_admin": True},
        service=_TaskLinksService(),
    )

    payload = json.loads(response.body)
    tasks = payload["tasks"]
    assert tasks[0]["url"] == f"{expected_scheme}://openrag.test/indexer/task/task-completed"
    assert tasks[1]["url"] == f"{expected_scheme}://openrag.test/indexer/task/task-failed"
    assert tasks[1]["error_url"] == f"{expected_scheme}://openrag.test/indexer/task/task-failed/error"


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

@pytest.mark.parametrize(
    ("preferred_url_scheme", "request_scheme", "expected_scheme"),
    [
        ("https", "http", "https"),
        (None, "http", "http"),
    ],
)
@pytest.mark.asyncio
async def test_task_status_error_url_uses_configured_or_request_scheme(
    preferred_url_scheme: str | None,
    request_scheme: str,
    expected_scheme: str,
) -> None:

    response = await get_task_status(
        request=_Request(scheme=request_scheme),
        task_id="task-failed",
        task_details={},
        config=SimpleNamespace(
            server=SimpleNamespace(
                preferred_url_scheme=preferred_url_scheme
            )
        ),
        service=_TaskStatusService(),
    )

    payload = json.loads(response.body)

    assert payload["error_url"] == (
        f"{expected_scheme}://openrag.test/indexer/task/task-failed/error"
    )