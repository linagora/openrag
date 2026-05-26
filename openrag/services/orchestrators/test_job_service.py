"""Unit tests for :class:`JobService` (Phase 8D.2)."""

from __future__ import annotations

import sys
import types

import pytest
from services.orchestrators.job_service import JobService


@pytest.fixture(autouse=True)
def _stub_ray_utils(monkeypatch):
    async def _call_ray_actor_with_timeout(*, future, timeout, task_description):
        return await future

    ray_utils = types.ModuleType("services.workers.ray_utils")
    ray_utils.call_ray_actor_with_timeout = _call_ray_actor_with_timeout
    monkeypatch.setitem(sys.modules, "services.workers.ray_utils", ray_utils)


class _Remote:
    """Mimics a Ray actor method: ``actor.method.remote(...)`` awaitable."""

    def __init__(self, fn):
        self._fn = fn

    def remote(self, *args, **kwargs):
        async def _coro():
            return self._fn(*args, **kwargs)

        return _coro()


class FakeTSM:
    def __init__(self, *, states=None, info=None, pool=None):
        self._states = states or {}
        self._info = info or {}
        self._pool = pool or {"total_capacity": 8, "pool_size": 2, "max_tasks_per_worker": 4}
        self.get_all_states = _Remote(lambda: dict(self._states))
        self.get_pool_info = _Remote(lambda: dict(self._pool))
        self.get_all_info = _Remote(lambda: dict(self._info))
        self.get_all_user_info = _Remote(lambda uid: {k: v for k, v in self._info.items() if v.get("user") == uid})
        self.get_details = _Remote(lambda task_id: self._info.get(task_id, {}).get("details"))
        self.get_user_pending_task_count = _Remote(
            lambda user_id: sum(1 for info in self._info.values() if info.get("user_id") == user_id)
        )


@pytest.mark.asyncio
async def test_get_queue_info_rolls_up_states():
    tsm = FakeTSM(
        states={
            "a": "QUEUED",
            "b": "CHUNKING",
            "c": "COMPLETED",
            "d": "FAILED",
            "e": "CANCELLED",
        }
    )
    out = await JobService(tsm).get_queue_info()

    assert out["workers"] == {"total_slots": 8, "pool_size": 2, "max_per_actor": 4}
    tasks = out["tasks"]
    assert tasks["active"] == 2
    assert tasks["active_statuses"] == {"QUEUED": 1, "SERIALIZING": 0, "CHUNKING": 1, "INSERTING": 0}
    assert tasks["total_completed"] == 1
    assert tasks["total_failed"] == 1
    assert tasks["total_cancelled"] == 1


@pytest.mark.asyncio
async def test_list_tasks_admin_sees_all():
    info = {
        "t1": {"state": "QUEUED", "details": {"f": 1}, "user": 1},
        "t2": {"state": "COMPLETED", "details": {"f": 2}, "user": 2},
    }
    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)
    assert {r["task_id"] for r in rows} == {"t1", "t2"}
    assert rows[0]["details"] == {"f": 1}


@pytest.mark.asyncio
async def test_list_tasks_user_scoped():
    info = {
        "t1": {"state": "QUEUED", "details": {}, "user": 1},
        "t2": {"state": "QUEUED", "details": {}, "user": 2},
    }
    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=False, user_id=1)
    assert [r["task_id"] for r in rows] == ["t1"]


@pytest.mark.asyncio
async def test_list_tasks_active_filter():
    info = {
        "t1": {"state": "QUEUED", "details": {}, "user": 1},
        "t2": {"state": "COMPLETED", "details": {}, "user": 1},
        "t3": {"state": "INSERTING", "details": {}, "user": 1},
    }
    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1, task_status="active")
    assert sorted(r["task_id"] for r in rows) == ["t1", "t3"]


@pytest.mark.asyncio
async def test_list_tasks_exact_status_case_insensitive():
    info = {
        "t1": {"state": "FAILED", "details": {}, "user": 1},
        "t2": {"state": "COMPLETED", "details": {}, "user": 1},
    }
    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1, task_status="failed")
    assert [r["task_id"] for r in rows] == ["t1"]


@pytest.mark.asyncio
async def test_get_task_details_uses_task_state_manager():
    info = {"t1": {"details": {"user_id": 7, "filename": "a.pdf"}}}

    details = await JobService(FakeTSM(info=info)).get_task_details("t1")

    assert details == {"user_id": 7, "filename": "a.pdf"}


@pytest.mark.asyncio
async def test_get_user_pending_task_count_uses_task_state_manager():
    info = {
        "t1": {"user_id": 7},
        "t2": {"user_id": 8},
        "t3": {"user_id": 7},
    }

    pending = await JobService(FakeTSM(info=info)).get_user_pending_task_count(7)

    assert pending == 2
