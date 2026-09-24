"""Unit tests for :class:`JobService` (Phase 8D.2)."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest
from services.orchestrators.job_service import JobService


@pytest.fixture(autouse=True)
def _stub_ray_utils(monkeypatch):
    async def _retry_idempotent_ray_actor_method(*, submit, recovery_timeout, task_description):
        return await submit()

    ray_utils = types.ModuleType("services.workers.ray_utils")
    ray_utils.retry_idempotent_ray_actor_method = _retry_idempotent_ray_actor_method
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
            "b": "SERIALIZING",
            "c": "COMPLETED",
            "d": "FAILED",
            "e": "CANCELLED",
        }
    )
    out = await JobService(tsm).get_queue_info()

    assert out["workers"] == {"total_slots": 8, "pool_size": 2, "max_per_actor": 4}
    tasks = out["tasks"]
    assert tasks["active"] == 2
    assert tasks["active_statuses"] == {"QUEUED": 1, "SERIALIZING": 1}
    assert tasks["total_completed"] == 1
    assert tasks["total_failed"] == 1
    assert tasks["total_cancelled"] == 1


@pytest.mark.asyncio
async def test_list_tasks_admin_sees_all():
    info = {
        "t1": {
            "state": "QUEUED",
            "details": {"f": 1},
            "user": 1,
            "created_at": "2026-07-20T08:00:00+00:00",
            "duration_ms": 1200,
        },
        "t2": {"state": "COMPLETED", "details": {"f": 2}, "user": 2},
    }
    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)
    assert {r["task_id"] for r in rows} == {"t1", "t2"}
    assert rows[0]["details"] == {"f": 1}
    assert rows[0]["created_at"] == "2026-07-20T08:00:00+00:00"
    assert rows[0]["duration_ms"] == 1200
    assert rows[1]["created_at"] is None
    assert rows[1]["duration_ms"] is None
    assert rows[1]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_list_tasks_reports_live_degraded_completion() -> None:
    info = {
        "t1": {
            "state": "COMPLETED",
            "details": {
                "file_id": "file-1",
                "degraded_stages": ["caption", "provider error must not escape"],
            },
            "user": 1,
        }
    }

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows[0]["outcome"] == "completed_degraded"
    assert rows[0]["details"]["degraded_stages"] == ["caption"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["QUEUED", "SERIALIZING"])
async def test_list_tasks_maps_active_states_to_one_bounded_outcome(state: str) -> None:
    info = {"t1": {"state": state, "details": {}, "user": 1}}

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows[0]["outcome"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_state", ["CHUNKING", "INSERTING"])
async def test_list_tasks_normalizes_legacy_active_states_at_the_public_boundary(legacy_state: str) -> None:
    info = {"t1": {"state": legacy_state, "details": {}, "user": 1}}

    rows = await JobService(FakeTSM(info=info)).list_tasks(
        is_admin=True,
        user_id=1,
        task_status="active",
    )

    assert [(row["state"], row["outcome"]) for row in rows] == [("SERIALIZING", "active")]


@pytest.mark.asyncio
async def test_list_tasks_uses_legacy_actor_timing_metadata_without_exposing_it():
    info = {
        "t1": {
            "state": "COMPLETED",
            "details": {
                "file_id": "file-1",
                "metadata": {
                    "filename": "report.pdf",
                    "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
                    "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
                },
            },
            "user": 1,
        }
    }

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows == [
        {
            "task_id": "t1",
            "state": "COMPLETED",
            "outcome": "completed",
            "details": {
                "file_id": "file-1",
                "metadata": {"filename": "report.pdf"},
            },
            "created_at": "2026-07-20T08:00:00+00:00",
            "duration_ms": 65_000,
        }
    ]


@pytest.mark.asyncio
async def test_list_tasks_computes_running_duration_from_legacy_actor_metadata():
    info = {
        "t1": {
            "state": "SERIALIZING",
            "details": {
                "metadata": {
                    "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
                }
            },
            "user": 1,
        }
    }
    service = JobService(FakeTSM(info=info))
    service._now = lambda: datetime(2026, 7, 20, 8, 0, 12, tzinfo=UTC)

    rows = await service.list_tasks(is_admin=True, user_id=1)

    assert rows[0]["duration_ms"] == 12_000


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
        "t3": {"state": "SERIALIZING", "details": {}, "user": 1},
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
async def test_list_tasks_gives_admins_a_bounded_failure_summary():
    info = {
        "t1": {
            "state": "FAILED",
            "details": {},
            "user": 1,
            "error": (
                "Traceback (most recent call last):\n"
                '  File "/srv/openrag/worker.py", line 10, in run\n'
                "ValueError:   parser   failed\n"
            ),
        }
    }

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows[0]["error_summary"] == "ValueError: parser failed"


@pytest.mark.asyncio
async def test_list_tasks_does_not_expose_failure_details_to_regular_users():
    info = {
        "t1": {
            "state": "FAILED",
            "details": {},
            "user": 1,
            "error": "RuntimeError: internal host failed",
        }
    }

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=False, user_id=1)

    assert "error_summary" not in rows[0]


@pytest.mark.asyncio
async def test_get_task_details_uses_task_state_manager():
    info = {
        "t1": {
            "details": {
                "user_id": 7,
                "filename": "a.pdf",
                "metadata": {
                    "language": "en",
                    "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
                    "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
                },
            }
        }
    }

    details = await JobService(FakeTSM(info=info)).get_task_details("t1")

    assert details == {"user_id": 7, "filename": "a.pdf", "metadata": {"language": "en"}}


@pytest.mark.asyncio
async def test_list_tasks_tolerates_malformed_legacy_metadata():
    info = {"t1": {"state": "COMPLETED", "details": {"metadata": "legacy"}, "user": 1}}

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows[0]["details"] == {"metadata": "legacy"}
    assert rows[0]["created_at"] is None
    assert rows[0]["duration_ms"] is None


@pytest.mark.asyncio
async def test_get_user_pending_task_count_uses_task_state_manager():
    info = {
        "t1": {"user_id": 7},
        "t2": {"user_id": 8},
        "t3": {"user_id": 7},
    }

    pending = await JobService(FakeTSM(info=info)).get_user_pending_task_count(7)

    assert pending == 2


class FakeJobRepo:
    """Durable job rows, as the actor would never return them."""

    def __init__(self, jobs=None, *, counts=None, broken: bool = False, fail_get_jobs: bool = False):
        self._jobs = {job.id: job for job in (jobs or [])}
        self._counts = counts
        self._broken = broken
        self._fail_get_jobs = fail_get_jobs
        self.listed_statuses = []
        self.calls: list[str] = []

    async def list_jobs(self, *, statuses=None, user_id=None, offset=0, limit=50):
        if self._broken:
            raise RuntimeError("jobs table is missing")
        self.listed_statuses.append(statuses)
        return [
            job
            for job in self._jobs.values()
            if (user_id is None or job.user_id == user_id) and (statuses is None or job.status.value in statuses)
        ]

    async def get_job(self, job_id):
        if self._broken:
            raise RuntimeError("jobs table is missing")
        return self._jobs.get(job_id)

    async def get_jobs(self, job_ids):
        self.calls.append("get_jobs")
        if self._broken or self._fail_get_jobs:
            raise RuntimeError("jobs table is missing")
        return [self._jobs[task_id] for task_id in job_ids if task_id in self._jobs]

    async def get_job_states(self, *, statuses=None, job_ids=None):
        self.calls.append("get_job_states")
        if self._broken:
            raise RuntimeError("jobs table is missing")
        return {
            job.id: job.status.value
            for job in self._jobs.values()
            if (statuses is None or job.status.value in statuses) and (job_ids is None or job.id in job_ids)
        }

    async def count_jobs(self):
        self.calls.append("count_jobs")
        if self._broken:
            raise RuntimeError("jobs table is missing")
        if self._counts is not None:
            return dict(self._counts)
        counts = {}
        for job in self._jobs.values():
            counts[job.status.value] = counts.get(job.status.value, 0) + 1
        return counts


def _job(**kwargs):
    from core.models.catalog import DocumentStatus, IndexationJob

    base = {
        "id": "t-old",
        "status": DocumentStatus.COMPLETED,
        "partition": "tenant-a",
        "file_id": "file-1",
        "filename": "report.pdf",
        "user_id": 7,
        "created_at": datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        "completed_at": datetime(2026, 9, 1, 10, 0, 30, tzinfo=UTC),
    }
    base.update(kwargs)
    return IndexationJob(**base)


@pytest.mark.asyncio
async def test_list_tasks_includes_jobs_the_actor_has_forgotten():
    info = {"t-live": {"state": "QUEUED", "details": {}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo([_job()]))

    rows = await service.list_tasks(is_admin=True, user_id=7)

    by_id = {row["task_id"]: row for row in rows}
    assert set(by_id) == {"t-live", "t-old"}
    assert by_id["t-old"]["state"] == "COMPLETED"
    assert by_id["t-old"]["duration_ms"] == 30_000


@pytest.mark.asyncio
async def test_list_tasks_reports_durable_degraded_completion() -> None:
    service = JobService(
        FakeTSM(info={}),
        job_repo=FakeJobRepo([_job(degraded_stages=["contextualize"])]),
    )

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert rows[0]["outcome"] == "completed_degraded"
    assert rows[0]["details"]["degraded_stages"] == ["contextualize"]


@pytest.mark.asyncio
async def test_list_tasks_summarizes_durable_failures_without_an_extra_lookup() -> None:
    from core.models.catalog import DocumentStatus

    service = JobService(
        FakeTSM(info={}),
        job_repo=FakeJobRepo(
            [
                _job(
                    status=DocumentStatus.FAILED,
                    error="Traceback (most recent call last):\nRuntimeError: durable failure",
                )
            ]
        ),
    )

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert rows[0]["error_summary"] == "RuntimeError: durable failure"


@pytest.mark.asyncio
async def test_list_tasks_prefers_the_stored_durable_failure_reason() -> None:
    from core.models.catalog import DocumentStatus

    service = JobService(
        FakeTSM(info={}),
        job_repo=FakeJobRepo(
            [
                _job(
                    status=DocumentStatus.FAILED,
                    error="Traceback (most recent call last):\nValueError: legacy fallback",
                    error_reason="RuntimeError: canonical failure",
                )
            ]
        ),
    )

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert rows[0]["error_summary"] == "RuntimeError: canonical failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "expected"),
    [("failed", ["FAILED"]), ("active", ["QUEUED", "SERIALIZING"]), (None, None)],
    ids=["exact", "active", "unfiltered"],
)
async def test_a_status_query_is_filtered_before_the_row_limit(task_status, expected):
    # The durable read is capped, so filtering it afterwards would drop matches
    # that sit behind newer rows of another status.
    from core.models.catalog import DocumentStatus

    repo = FakeJobRepo([_job(id="t-failed", status=DocumentStatus.FAILED)])
    service = JobService(FakeTSM(info={}), job_repo=repo)

    await service.list_tasks(is_admin=True, user_id=7, task_status=task_status)

    assert repo.listed_statuses == [expected]


@pytest.mark.asyncio
async def test_durable_state_wins_over_live_actor_row():
    info = {"t1": {"state": "SERIALIZING", "details": {}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo([_job(id="t1")]))

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert [row["state"] for row in rows] == ["COMPLETED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_state", ["COMPLETED", "FAILED", "CANCELLED"])
async def test_live_terminal_state_wins_over_stale_durable_active_row(actor_state: str):
    from core.models.catalog import DocumentStatus

    actor_info = {"t1": {"state": actor_state, "details": {}, "user": 7}}
    service = JobService(
        FakeTSM(info=actor_info),
        job_repo=FakeJobRepo([_job(id="t1", status=DocumentStatus.SERIALIZING)]),
    )

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert [row["state"] for row in rows] == [actor_state]


@pytest.mark.asyncio
async def test_list_tasks_preserves_live_terminal_timing_and_degradation():
    from core.models.catalog import DocumentStatus

    info = {
        "t1": {
            "state": "COMPLETED",
            "details": {
                "degraded_stages": ["caption"],
                "metadata": {
                    "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
                    "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
                },
            },
            "user": 7,
            "duration_ms": 65_000,
        }
    }
    service = JobService(
        FakeTSM(info=info),
        job_repo=FakeJobRepo([_job(id="t1", status=DocumentStatus.SERIALIZING, completed_at=None)]),
    )

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert rows[0]["outcome"] == "completed_degraded"
    assert rows[0]["duration_ms"] == 65_000
    assert rows[0]["details"]["degraded_stages"] == ["caption"]


@pytest.mark.asyncio
async def test_durable_state_wins_even_when_the_status_filter_excludes_it():
    info = {"t1": {"state": "SERIALIZING", "details": {}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo([_job(id="t1")]))

    rows = await service.list_tasks(is_admin=True, user_id=7, task_status="active")

    assert rows == []


@pytest.mark.asyncio
async def test_get_task_details_prefers_the_durable_row():
    info = {
        "t-old": {
            "state": "SERIALIZING",
            "details": {
                "file_id": "stale-file",
                "partition": "stale-tenant",
                "metadata": {"filename": "report.pdf"},
                "user_id": 7,
            },
            "user": 7,
        }
    }
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo([_job()]))

    details = await service.get_task_details("t-old")

    assert details == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.pdf"},
        "user_id": 7,
        "degraded_stages": [],
    }


@pytest.mark.asyncio
async def test_get_queue_info_merges_actor_only_tasks_without_overriding_durable_rows():
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"live": "QUEUED", "stale": "SERIALIZING"})
    in_flight = [_job(id=f"q{i}", status=DocumentStatus.QUEUED, completed_at=None) for i in range(2)]
    in_flight += [_job(id=f"s{i}", status=DocumentStatus.SERIALIZING, completed_at=None) for i in range(3)]
    repo = FakeJobRepo(
        [_job(id="stale"), *in_flight],
        counts={"QUEUED": 2, "SERIALIZING": 3, "COMPLETED": 4, "FAILED": 5, "CANCELLED": 6},
    )

    out = await JobService(tsm, job_repo=repo).get_queue_info()

    assert out["tasks"] == {
        "active": 6,
        "active_statuses": {"QUEUED": 3, "SERIALIZING": 3},
        "total_cancelled": 6,
        "total_completed": 4,
        "total_failed": 5,
    }


@pytest.mark.asyncio
async def test_get_queue_info_reconciles_live_terminal_state_against_durable_counts():
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"just-finished": "COMPLETED"})
    repo = FakeJobRepo(
        [_job(id="just-finished", status=DocumentStatus.SERIALIZING)],
        counts={"SERIALIZING": 1},
    )

    out = await JobService(tsm, job_repo=repo).get_queue_info()

    assert out["tasks"] == {
        "active": 0,
        "active_statuses": {"QUEUED": 0, "SERIALIZING": 0},
        "total_cancelled": 0,
        "total_completed": 1,
        "total_failed": 0,
    }


@pytest.mark.asyncio
async def test_get_queue_info_falls_back_to_actor_when_durable_counts_fail():
    tsm = FakeTSM(states={"live": "SERIALIZING", "done": "COMPLETED"})

    out = await JobService(tsm, job_repo=FakeJobRepo(broken=True)).get_queue_info()

    assert out["tasks"] == {
        "active": 1,
        "active_statuses": {"QUEUED": 0, "SERIALIZING": 1},
        "total_cancelled": 0,
        "total_completed": 1,
        "total_failed": 0,
    }


@pytest.mark.asyncio
async def test_active_task_counts_include_durable_rows_a_restarted_actor_forgot():
    """A restarted TaskStateManager starts empty; the backlog must not read as zero."""
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={})
    repo = FakeJobRepo(
        [
            _job(id="q1", status=DocumentStatus.QUEUED, completed_at=None),
            _job(id="q2", status=DocumentStatus.QUEUED, completed_at=None),
            _job(id="s1", status=DocumentStatus.SERIALIZING, completed_at=None),
            _job(id="done"),
        ]
    )

    counts = await JobService(tsm, job_repo=repo).get_active_task_counts()

    assert counts == {"QUEUED": 2, "SERIALIZING": 1}


@pytest.mark.asyncio
async def test_active_task_counts_reconcile_like_get_queue_info():
    """Actor-only tasks add to the durable counts; a live terminal state wins over a stale durable one."""
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"live": "QUEUED", "just-finished": "COMPLETED"})
    repo = FakeJobRepo(
        [
            _job(id="just-finished", status=DocumentStatus.SERIALIZING, completed_at=None),
            _job(id="waiting", status=DocumentStatus.QUEUED, completed_at=None),
        ]
    )
    service = JobService(tsm, job_repo=repo)

    counts = await service.get_active_task_counts()

    assert counts == {"QUEUED": 2, "SERIALIZING": 0}
    assert counts == (await service.get_queue_info())["tasks"]["active_statuses"]


class _RowWrittenBetweenReads(FakeJobRepo):
    """Task ``x`` is committed after the status read and before the ID read.

    ``count_jobs`` is the snapshot taken before ``x`` existed; ``get_jobs`` and
    the ID read already see it, still QUEUED, while the actor has moved on.
    """

    async def get_job_states(self, *, statuses=None, job_ids=None):
        self.calls.append("get_job_states")
        if statuses is not None:
            return {}
        return {"x": "QUEUED"} if "x" in job_ids else {}


def _row_written_between_reads():
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"x": "SERIALIZING"})
    repo = _RowWrittenBetweenReads(
        [_job(id="x", status=DocumentStatus.QUEUED, completed_at=None)], counts={"COMPLETED": 5}
    )
    return JobService(tsm, job_repo=repo)


@pytest.mark.asyncio
async def test_active_task_counts_never_subtract_a_row_the_first_read_missed():
    counts = await _row_written_between_reads().get_active_task_counts()

    assert counts == {"QUEUED": 0, "SERIALIZING": 1}


@pytest.mark.asyncio
async def test_queue_info_active_counts_never_go_negative():
    tasks = (await _row_written_between_reads().get_queue_info())["tasks"]

    assert tasks["active_statuses"] == {"QUEUED": 0, "SERIALIZING": 1}
    assert tasks["active"] == 1
    assert tasks["total_completed"] == 5


@pytest.mark.asyncio
async def test_active_task_counts_read_only_in_flight_rows():
    """The scrape runs this every interval: no whole-table count, no full rows."""
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"live": "QUEUED", "done": "COMPLETED"})
    repo = FakeJobRepo([_job(id="waiting", status=DocumentStatus.QUEUED, completed_at=None), _job(id="done")])

    counts = await JobService(tsm, job_repo=repo).get_active_task_counts()

    assert counts == {"QUEUED": 2, "SERIALIZING": 0}
    assert repo.calls == ["get_job_states", "get_job_states"]


@pytest.mark.asyncio
async def test_active_task_counts_ignore_a_live_task_already_settled_durably():
    """An actor row that trails a durable terminal write is not in flight."""
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"late": "SERIALIZING"})
    repo = FakeJobRepo([_job(id="late", status=DocumentStatus.COMPLETED)])

    counts = await JobService(tsm, job_repo=repo).get_active_task_counts()

    assert counts == {"QUEUED": 0, "SERIALIZING": 0}


@pytest.mark.asyncio
async def test_active_task_counts_fall_back_to_actor_when_durable_counts_fail():
    tsm = FakeTSM(states={"live": "SERIALIZING", "done": "COMPLETED"})

    counts = await JobService(tsm, job_repo=FakeJobRepo(broken=True)).get_active_task_counts()

    assert counts == {"QUEUED": 0, "SERIALIZING": 1}


@pytest.mark.asyncio
async def test_get_queue_info_falls_back_when_durable_actor_lookup_fails():
    from core.models.catalog import DocumentStatus

    tsm = FakeTSM(states={"live": "SERIALIZING", "done": "COMPLETED"})
    repo = FakeJobRepo(
        [_job(id="live", status=DocumentStatus.SERIALIZING, completed_at=None)],
        counts={"SERIALIZING": 1, "COMPLETED": 4},
        fail_get_jobs=True,
    )

    out = await JobService(tsm, job_repo=repo).get_queue_info()

    assert out["tasks"] == {
        "active": 1,
        "active_statuses": {"QUEUED": 0, "SERIALIZING": 1},
        "total_cancelled": 0,
        "total_completed": 1,
        "total_failed": 0,
    }


@pytest.mark.asyncio
async def test_list_tasks_preserves_actor_metadata_with_durable_state():
    info = {
        "t1": {
            "state": "SERIALIZING",
            "details": {"metadata": {"filename": "report.pdf"}, "user_id": 7},
            "user": 7,
        }
    }
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo([_job(id="t1")]))

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert rows[0]["state"] == "COMPLETED"
    assert rows[0]["details"]["metadata"] == {"filename": "report.pdf"}


@pytest.mark.asyncio
async def test_get_task_details_falls_back_to_the_durable_row():
    service = JobService(FakeTSM(info={}), job_repo=FakeJobRepo([_job()]))

    details = await service.get_task_details("t-old")

    assert details == {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.pdf"},
        "user_id": 7,
        "degraded_stages": [],
    }


@pytest.mark.asyncio
async def test_get_task_details_preserves_live_terminal_degradation():
    from core.models.catalog import DocumentStatus

    info = {
        "t1": {
            "state": "COMPLETED",
            "details": {"degraded_stages": ["caption"], "metadata": {"filename": "report.pdf"}},
            "user": 7,
        }
    }
    tsm = FakeTSM(info=info)
    tsm.get_state = _Remote(lambda _task_id: "COMPLETED")
    service = JobService(
        tsm,
        job_repo=FakeJobRepo([_job(id="t1", status=DocumentStatus.SERIALIZING, completed_at=None)]),
    )

    details = await service.get_task_details("t1")

    assert details["degraded_stages"] == ["caption"]
    assert details["metadata"] == {"filename": "report.pdf"}


@pytest.mark.asyncio
async def test_get_task_details_keeps_actor_details_when_state_lookup_fails():
    from core.models.catalog import DocumentStatus

    info = {
        "t1": {
            "details": {"degraded_stages": ["caption"], "metadata": {"filename": "report.pdf"}},
            "user": 7,
        }
    }
    service = JobService(
        FakeTSM(info=info),
        job_repo=FakeJobRepo([_job(id="t1", status=DocumentStatus.SERIALIZING, completed_at=None, filename=None)]),
    )

    details = await service.get_task_details("t1")

    assert details["metadata"] == {"filename": "report.pdf"}


@pytest.mark.asyncio
async def test_queue_views_survive_an_unavailable_jobs_table():
    info = {"t-live": {"state": "QUEUED", "details": {}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo(broken=True))

    rows = await service.list_tasks(is_admin=True, user_id=7)

    assert [row["task_id"] for row in rows] == ["t-live"]
    assert await service.get_task_details("t-live") == {}
