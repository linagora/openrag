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


# ---------------------------------------------------------------------------
# Durable reads (issue #660) — Postgres is the source of truth, the actor is
# a hot cache whose terminal entries are evicted.
# ---------------------------------------------------------------------------


class FakeJobRepo:
    def __init__(self, jobs=None, counts=None, pending=0, boom=False):
        self._jobs = jobs or []
        self._counts = counts or {}
        self._pending = pending
        self._boom = boom
        self.list_calls: list[dict] = []

    def _check(self):
        if self._boom:
            raise RuntimeError("postgres down")

    async def list_jobs(self, status=None, offset=0, limit=50, user_id=None):
        self._check()
        self.list_calls.append({"status": status, "offset": offset, "limit": limit, "user_id": user_id})
        return list(self._jobs)

    async def get_job(self, job_id):
        self._check()
        return next((j for j in self._jobs if j.id == job_id), None)

    async def count_by_status(self):
        self._check()
        return dict(self._counts)


def _job(**kwargs):
    from core.models.catalog import IndexationJob

    base = {"id": "t1", "partition": "p", "file_id": "f1", "user_id": 7, "job_metadata": {"filename": "a.pdf"}}
    base.update(kwargs)
    return IndexationJob(**base)


@pytest.mark.asyncio
async def test_get_queue_info_counts_come_from_postgres_when_available():
    repo = FakeJobRepo(counts={"QUEUED": 2, "COMPLETED": 5, "FAILED": 1})
    svc = JobService(task_state_manager=FakeTSM(states={"zombie": "QUEUED"}), job_repo=repo)

    out = await svc.get_queue_info()

    assert out["tasks"]["active"] == 2
    assert out["tasks"]["total_completed"] == 5
    assert out["tasks"]["total_failed"] == 1
    # pool info still comes from the actor
    assert out["workers"]["pool_size"] == 2


@pytest.mark.asyncio
async def test_get_queue_info_falls_back_to_the_actor_when_postgres_is_down():
    svc = JobService(task_state_manager=FakeTSM(states={"a": "QUEUED"}), job_repo=FakeJobRepo(boom=True))

    out = await svc.get_queue_info()

    assert out["tasks"]["active"] == 1


@pytest.mark.asyncio
async def test_list_tasks_reads_durable_jobs_and_scopes_non_admins():
    from core.models.catalog import DocumentStatus

    repo = FakeJobRepo(jobs=[_job(status=DocumentStatus.COMPLETED)])
    svc = JobService(task_state_manager=FakeTSM(), job_repo=repo)

    rows = await svc.list_tasks(is_admin=False, user_id=7, task_status="completed")

    assert rows == [
        {
            "task_id": "t1",
            "state": "COMPLETED",
            "details": {"file_id": "f1", "partition": "p", "metadata": {"filename": "a.pdf"}, "user_id": 7},
        }
    ]
    assert repo.list_calls[0]["user_id"] == 7
    assert repo.list_calls[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_list_tasks_does_not_scope_admins_to_their_own_jobs():
    repo = FakeJobRepo(jobs=[])
    await JobService(task_state_manager=FakeTSM(), job_repo=repo).list_tasks(is_admin=True, user_id=1)

    assert repo.list_calls[0]["user_id"] is None


@pytest.mark.asyncio
async def test_list_tasks_falls_back_to_the_actor_when_postgres_is_down():
    info = {"t1": {"state": "FAILED", "details": {"file_id": "f"}, "user": 1}}
    svc = JobService(task_state_manager=FakeTSM(info=info), job_repo=FakeJobRepo(boom=True))

    rows = await svc.list_tasks(is_admin=True, user_id=1)

    assert rows == [{"task_id": "t1", "state": "FAILED", "details": {"file_id": "f"}}]


@pytest.mark.asyncio
async def test_get_task_details_survives_a_restart_via_postgres():
    svc = JobService(task_state_manager=FakeTSM(), job_repo=FakeJobRepo(jobs=[_job()]))

    assert await svc.get_task_details("t1") == {
        "file_id": "f1",
        "partition": "p",
        "metadata": {"filename": "a.pdf"},
        "user_id": 7,
    }


@pytest.mark.asyncio
async def test_get_user_pending_task_count_stays_on_the_in_memory_cache():
    """The quota gate must not be jammable by an orphaned job row.

    A durable job leaves the active states only when a worker writes a terminal
    transition, so a crash mid-dispatch would hold the user's quota open-endedly
    (retention sweeps terminal rows only). See the method docstring / #664.
    """
    info = {"t1": {"state": "QUEUED", "details": {}, "user_id": 7}}
    repo = FakeJobRepo(pending=99)
    svc = JobService(task_state_manager=FakeTSM(info=info), job_repo=repo)

    assert await svc.get_user_pending_task_count(7) == 1


@pytest.mark.asyncio
async def test_list_tasks_fails_closed_for_an_anonymous_non_admin():
    """list_jobs(user_id=None) means *every* job — never hand that to a user.

    The durable read scopes with user_id=None if is_admin else user_id, so a
    non-admin arriving without an id would select the whole table. Guard here
    rather than trust every caller to have resolved one.
    """
    repo = FakeJobRepo(jobs=[_job(id="t1", user_id=1), _job(id="t2", user_id=2)])
    svc = JobService(task_state_manager=FakeTSM(), job_repo=repo)

    assert await svc.list_tasks(is_admin=False, user_id=None) == []
    assert repo.list_calls == []
