"""Unit tests for :class:`JobService` (Phase 8D.2)."""

from __future__ import annotations

import sys
import types
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

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
    def __init__(self, *, states=None, info=None, pool=None, stage_snapshots=None):
        self._states = states or {}
        self._info = info or {}
        self._stage_snapshots = stage_snapshots or {}
        self.stage_snapshot_bulk_reads = 0
        self.stage_snapshot_reads = 0
        self._pool = pool or {"total_capacity": 8, "pool_size": 2, "max_tasks_per_worker": 4}
        self.get_all_states = _Remote(lambda: dict(self._states))
        self.get_state = _Remote(lambda task_id: self._states.get(task_id))
        self.get_pool_info = _Remote(lambda: dict(self._pool))
        self.get_all_info = _Remote(self._list_info)
        self.get_all_user_info = _Remote(lambda uid: self._list_info(uid))
        self.get_details = _Remote(lambda task_id: self._info.get(task_id, {}).get("details"))
        self.get_user_pending_task_count = _Remote(
            lambda user_id: sum(1 for info in self._info.values() if info.get("user_id") == user_id)
        )
        self.get_stage_progress_snapshots = _Remote(self._bulk_stage_snapshots)
        self.get_stage_progress_snapshot = _Remote(self._stage_snapshot)

    def _stage_snapshot(self, task_id):
        self.stage_snapshot_reads += 1
        return self._stage_snapshots.get(task_id)

    def _list_info(self, user_id=None):
        rows = {
            task_id: dict(info)
            for task_id, info in self._info.items()
            if user_id is None or info.get("user") == user_id
        }
        for task_id, snapshot in self._stage_snapshots.items():
            if task_id in rows:
                rows[task_id]["stage_progress"] = snapshot.model_copy(update={"event_tail": []})
        return rows

    def _bulk_stage_snapshots(self, task_ids):
        self.stage_snapshot_bulk_reads += 1
        return {
            task_id: snapshot.model_copy(update={"event_tail": []})
            for task_id, snapshot in self._stage_snapshots.items()
            if task_id in task_ids
        }


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
async def test_list_tasks_ignores_stateless_actor_records():
    info = {"stateless": {"state": None, "details": {}, "user": 1}}

    rows = await JobService(FakeTSM(info=info)).list_tasks(is_admin=True, user_id=1)

    assert rows == []


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
async def test_list_tasks_admin_after_a_refused_duplicate_upload():
    """A registration refused by the admission fence must not break the admin list."""
    from services.workers.task_state import TaskStateManager

    manager = TaskStateManager.__ray_metadata__.modified_class()
    await manager.set_queued_details("task-a", file_id="file-1", partition="tenant-a", metadata={}, user_id=1)
    refused = await manager.set_queued_details_v2(
        "task-b",
        file_id="file-1",
        partition="tenant-a",
        metadata={},
        user_id=1,
        reject_if_file_active=True,
    )
    assert refused["accepted"] is False

    rows = await JobService(FakeTSM(info=await manager.get_all_info())).list_tasks(is_admin=True, user_id=1)

    assert [(row["task_id"], row["state"]) for row in rows] == [("task-a", "QUEUED")]


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
async def test_list_tasks_orders_durable_progress_before_client_pagination() -> None:
    from core.models.stage_progress import (
        DurableStageProgress,
        StageProgressSummary,
        StageTaskPhase,
    )
    from services.workers.stage_tracker import StageTracker

    durable = {}
    live = {}
    for task_id, stage, rank in (("parse", "parse", 0), ("embed", "embed", 5)):
        tracker = StageTracker(task_id)
        tracker.start_stage(stage, rank)
        summary = tracker.summary()
        durable[task_id] = DurableStageProgress.model_validate(
            {**summary.model_dump(), "persisted_through_sequence": summary.latest_sequence}
        )
        live[task_id] = tracker.snapshot()
    finalizing_summary = StageProgressSummary(
        task_id="finalizing",
        phase=StageTaskPhase.FINALIZING,
        position_score=7000,
    )
    durable["finalizing"] = DurableStageProgress.model_validate(
        {**finalizing_summary.model_dump(), "persisted_through_sequence": 0}
    )
    live["embed"] = live["parse"]
    info = {
        task_id: {"state": state, "details": {}, "user": 1, "stage_tracking_version": 1}
        for task_id, state in (
            ("queued", "QUEUED"),
            ("unknown", "SERIALIZING"),
            ("parse", "SERIALIZING"),
            ("embed", "SERIALIZING"),
            ("finalizing", "SERIALIZING"),
            ("terminal", "COMPLETED"),
        )
    }
    info["unknown"]["stage_tracking_version"] = 1
    tsm = FakeTSM(info=info, stage_snapshots=live)
    repo = FakeJobRepo(stage_summaries=durable)
    result = await JobService(tsm, job_repo=repo).list_tasks(is_admin=True, user_id=1, limit=50)
    rows = result["tasks"]

    assert [row["task_id"] for row in rows] == ["finalizing", "embed", "parse", "unknown", "queued", "terminal"]
    assert tsm.stage_snapshot_bulk_reads == 1
    assert tsm.stage_snapshot_reads == 0
    assert rows[0]["stage_progress"]["freshness"] == "durable"
    assert rows[1]["stage_progress"]["stage_id"] == "embed"
    assert rows[1]["stage_progress"]["elapsed_ms"] is None
    assert rows[1]["stage_progress"]["is_currently_running"] is False
    assert rows[2]["stage_progress"]["stage_id"] == "parse"
    assert rows[2]["stage_progress"]["freshness"] == "live"
    assert rows[2]["stage_progress"]["is_currently_running"] is True
    assert rows[3]["stage_progress"]["history_quality"] == "unknown"


@pytest.mark.asyncio
async def test_unpaginated_task_list_loads_stage_summaries_for_active_and_terminal_tasks() -> None:
    from core.models.stage_progress import DurableStageProgress

    summary = DurableStageProgress(task_id="task-1", current_stage_id="embed", current_stage_rank=5)
    queued_summary = DurableStageProgress(task_id="queued", current_stage_id="parse", current_stage_rank=0)
    completed_summary = DurableStageProgress(task_id="completed", current_stage_id="store", current_stage_rank=6)
    failed_summary = DurableStageProgress(task_id="failed", current_stage_id="embed", current_stage_rank=5)
    repo = FakeJobRepo(
        [
            _job(id="task-1", status="SERIALIZING", stage_tracking_version=1),
            _job(id="queued", status="QUEUED", stage_tracking_version=1),
            _job(id="completed", status="COMPLETED", stage_tracking_version=1),
            _job(id="failed", status="FAILED", stage_tracking_version=1),
        ],
        stage_summaries={
            "task-1": summary,
            "queued": queued_summary,
            "completed": completed_summary,
            "failed": failed_summary,
        },
    )
    tsm = FakeTSM(
        info={
            "task-1": {"state": "SERIALIZING", "details": {}, "user": 1},
            "queued": {"state": "QUEUED", "details": {}, "user": 1},
            "completed": {"state": "COMPLETED", "details": {}, "user": 1},
            "failed": {"state": "FAILED", "details": {}, "user": 1},
        }
    )

    rows = await JobService(tsm, job_repo=repo).list_tasks(is_admin=True, user_id=1)
    rows_by_id = {row["task_id"]: row for row in rows}

    assert rows_by_id["task-1"]["stage_progress"]["stage_id"] == "embed"
    assert "stage_progress" not in rows_by_id["queued"]
    assert rows_by_id["completed"]["stage_progress"]["stage_id"] == "store"
    assert rows_by_id["failed"]["stage_progress"]["stage_id"] == "embed"
    assert set(repo.stage_summary_requests[0]) == {"task-1", "completed", "failed"}


@pytest.mark.asyncio
async def test_list_tasks_sorts_complete_authorized_set_before_server_pagination() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.workers.stage_tracker import StageTracker

    info = {
        "queued": {"state": "QUEUED", "details": {"partition": "p2"}, "user": 1},
        "unknown": {"state": "SERIALIZING", "details": {"partition": "p1"}, "user": 1},
        "parse": {"state": "SERIALIZING", "details": {"partition": "p1"}, "user": 1},
        "embed": {"state": "SERIALIZING", "details": {"partition": "p2"}, "user": 1},
    }
    snapshots = {}
    durable = {}
    for task_id, stage, rank in (("parse", "parse", 0), ("embed", "embed", 5)):
        tracker = StageTracker(task_id)
        tracker.start_stage(stage, rank)
        snapshots[task_id] = tracker.snapshot()
        summary = tracker.summary()
        durable[task_id] = DurableStageProgress.model_validate(
            {**summary.model_dump(), "persisted_through_sequence": summary.latest_sequence}
        )
    service = JobService(
        FakeTSM(info=info, stage_snapshots=snapshots),
        job_repo=FakeJobRepo(stage_summaries=durable),
    )

    first = await service.list_tasks(is_admin=True, user_id=1, task_status="active", limit=2, offset=0)
    second = await service.list_tasks(is_admin=True, user_id=1, task_status="active", limit=2, offset=2)

    assert [row["task_id"] for row in first["tasks"]] == ["embed", "parse"]
    assert [row["task_id"] for row in second["tasks"]] == ["unknown", "queued"]
    assert first["pagination"] == {
        "offset": 0,
        "limit": 2,
        "total": 4,
        "has_more": True,
        "available_partitions": ["p1", "p2"],
    }
    assert second["pagination"]["has_more"] is False


@pytest.mark.asyncio
async def test_list_tasks_keeps_finished_tasks_newest_first() -> None:
    older = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    newer = older + timedelta(hours=1)
    repo = FakeJobRepo(
        [
            _job(id="finished-older", status="COMPLETED", created_at=older),
            _job(id="finished-newer", status="FAILED", created_at=newer),
        ]
    )

    rows = await JobService(FakeTSM(), job_repo=repo).list_tasks(is_admin=True, user_id=7)

    assert [row["task_id"] for row in rows] == ["finished-newer", "finished-older"]


@pytest.mark.asyncio
async def test_paginated_task_list_interleaves_actor_and_durable_tasks_by_stage_score() -> None:
    from core.models.stage_progress import DurableStageProgress, StageTaskPhase

    actor_info = {
        "actor-900": {"state": "SERIALIZING", "details": {"partition": "p"}, "user": 1},
        "actor-700": {"state": "SERIALIZING", "details": {"partition": "p"}, "user": 1},
        "actor-500": {"state": "SERIALIZING", "details": {"partition": "p"}, "user": 1},
    }
    jobs = [
        _job(id=task_id, status="SERIALIZING", stage_tracking_version=1)
        for task_id in ("actor-900", "actor-700", "actor-500", "durable-800", "durable-600", "durable-400")
    ]
    scores = {
        "actor-900": 900,
        "durable-800": 800,
        "actor-700": 700,
        "durable-600": 600,
        "actor-500": 500,
        "durable-400": 400,
    }
    summaries = {
        task_id: DurableStageProgress(
            task_id=task_id,
            latest_sequence=0,
            snapshot_revision=1,
            phase=StageTaskPhase.FINALIZING,
            position_score=score,
            persisted_through_sequence=0,
        )
        for task_id, score in scores.items()
    }
    service = JobService(FakeTSM(info=actor_info), job_repo=FakeJobRepo(jobs, stage_summaries=summaries))

    first = await service.list_tasks(is_admin=True, user_id=1, task_status="active", limit=2, offset=0)
    second = await service.list_tasks(is_admin=True, user_id=1, task_status="active", limit=2, offset=2)
    third = await service.list_tasks(is_admin=True, user_id=1, task_status="active", limit=2, offset=4)

    assert [row["task_id"] for page in (first, second, third) for row in page["tasks"]] == [
        "actor-900",
        "durable-800",
        "actor-700",
        "durable-600",
        "actor-500",
        "durable-400",
    ]


@pytest.mark.asyncio
async def test_paginated_task_list_preserves_snapshot_order_when_stage_progress_advances() -> None:
    from core.models.stage_progress import DurableStageProgress, StageTaskPhase

    def summary(task_id, score):
        return DurableStageProgress(
            task_id=task_id,
            latest_sequence=0,
            snapshot_revision=1,
            phase=StageTaskPhase.FINALIZING,
            position_score=score,
            persisted_through_sequence=0,
        )

    snapshot_summaries = {"task-a": summary("task-a", 900), "task-b": summary("task-b", 800)}
    advanced_summaries = {"task-a": summary("task-a", 100), "task-b": summary("task-b", 950)}

    class AdvancingRepo(FakeJobRepo):
        @asynccontextmanager
        async def task_list_snapshot(self):
            yield self
            self._stage_summaries = advanced_summaries

    repo = AdvancingRepo(
        [
            _job(id="task-a", status="SERIALIZING", stage_tracking_version=1),
            _job(id="task-b", status="SERIALIZING", stage_tracking_version=1),
        ],
        stage_summaries=snapshot_summaries,
    )

    result = await JobService(FakeTSM(), job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        task_status="active",
        limit=2,
        offset=0,
    )

    assert [task["task_id"] for task in result["tasks"]] == ["task-a", "task-b"]
    assert [task["stage_progress"]["position_score"] for task in result["tasks"]] == [100, 950]


@pytest.mark.asyncio
async def test_paginated_task_list_does_not_overfetch_for_durable_terminal_actor_rows() -> None:
    older = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    newer = older + timedelta(hours=1)
    actor_info = {
        "finished-older": {"state": "COMPLETED", "details": {"partition": "p"}, "user": 1},
        "finished-newer": {"state": "FAILED", "details": {"partition": "p"}, "user": 1},
        "actor-only-queued": {"state": "QUEUED", "details": {"partition": "p"}, "user": 1},
    }
    repo = FakeJobRepo(
        [
            _job(id="finished-older", status="COMPLETED", created_at=older),
            _job(id="finished-newer", status="FAILED", created_at=newer),
        ]
    )
    service = JobService(FakeTSM(info=actor_info), job_repo=repo)

    pages = [await service.list_tasks(is_admin=True, user_id=1, limit=1, offset=offset) for offset in range(3)]

    assert [page["tasks"][0]["task_id"] for page in pages] == [
        "actor-only-queued",
        "finished-newer",
        "finished-older",
    ]
    assert all(page["pagination"]["total"] == 3 for page in pages)
    assert repo.page_requests == [
        (0, 1, ["actor-only-queued"]),
        (1, 1, ["actor-only-queued"]),
        (2, 1, ["actor-only-queued"]),
    ]


@pytest.mark.asyncio
async def test_paginated_task_list_does_not_expand_window_for_matching_live_rows() -> None:
    created_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    jobs = [
        _job(
            id=f"queued-{index:03}",
            status="QUEUED",
            created_at=created_at + timedelta(seconds=index),
            partition="p",
        )
        for index in range(250)
    ]
    actor_info = {job.id: {"state": "QUEUED", "details": {"partition": "p"}, "user": 7} for job in jobs}
    repo = FakeJobRepo(jobs)

    result = await JobService(FakeTSM(info=actor_info), job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        limit=1,
        offset=0,
    )

    assert result["pagination"]["total"] == 250
    assert repo.page_requests == [(0, 1, [])]


@pytest.mark.asyncio
async def test_paginated_task_metadata_uses_one_durable_snapshot_for_actor_deltas() -> None:
    task_id = "settling-task"
    initial = _job(id=task_id, status="SERIALIZING", partition="p")
    settled = _job(id=task_id, status="FAILED", partition="p")

    class SettlingRepo(FakeJobRepo):
        async def get_jobs(self, job_ids):
            rows = await super().get_jobs(job_ids)
            self._jobs[task_id] = settled
            return rows

    repo = SettlingRepo([initial])
    tsm = FakeTSM(info={task_id: {"state": "FAILED", "details": {"partition": "p"}, "user": 7}})

    result = await JobService(tsm, job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        task_status="active",
        limit=10,
        offset=0,
    )

    assert result["tasks"] == []
    assert result["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_paginated_task_list_keeps_last_failed_stage_in_summary() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("failed-task")
    tracker.start_stage("embed", 5)
    tracker.finish_stage(error=RuntimeError("embedding failed"))
    tracker.finish_task()
    summary = DurableStageProgress.model_validate(
        {**tracker.summary().model_dump(), "persisted_through_sequence": tracker.summary().latest_sequence}
    )
    repo = FakeJobRepo(
        [_job(id="failed-task", status="FAILED", stage_tracking_version=1)],
        stage_summaries={"failed-task": summary},
        stage_events={"failed-task": tracker.snapshot().event_tail},
    )
    tsm = FakeTSM(info={"failed-task": {"state": "FAILED", "details": {}, "user": 7}})

    result = await JobService(tsm, job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        task_status="FAILED",
        limit=10,
        offset=0,
    )

    assert result["tasks"][0]["stage_progress"]["stage_id"] is None
    assert result["tasks"][0]["stage_progress"]["last_known_stage_id"] == "embed"


@pytest.mark.asyncio
async def test_paginated_task_list_survives_missing_last_known_stage_metadata() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("failed-task")
    tracker.start_stage("embed", 5)
    tracker.finish_stage(error=RuntimeError("embedding failed"))
    tracker.finish_task()
    tracker_summary = tracker.summary()
    summary = DurableStageProgress.model_validate(
        {**tracker_summary.model_dump(), "persisted_through_sequence": tracker_summary.latest_sequence}
    )
    repo = FakeJobRepo(
        [_job(id="failed-task", status="FAILED", stage_tracking_version=1)],
        stage_summaries={"failed-task": summary},
        stage_events={"failed-task": tracker.snapshot().event_tail},
        fail_latest_stage_ids=True,
    )
    tsm = FakeTSM(info={"failed-task": {"state": "FAILED", "details": {}, "user": 7}})

    result = await JobService(tsm, job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        task_status="FAILED",
        limit=10,
        offset=0,
    )

    assert [task["task_id"] for task in result["tasks"]] == ["failed-task"]
    assert result["tasks"][0]["stage_progress"]["stage_id"] is None
    assert result["tasks"][0]["stage_progress"].get("last_known_stage_id") is None


@pytest.mark.asyncio
async def test_slow_optional_stage_sidecar_does_not_block_task_state_snapshot_fallback(monkeypatch) -> None:
    import asyncio

    from services.orchestrators import job_service

    monkeypatch.setattr(job_service, "_OPTIONAL_STAGE_PROGRESS_SIDECAR_TIMEOUT_SECONDS", 0.01)
    tsm = FakeTSM(info={"task-1": {"state": "SERIALIZING", "details": {}}})

    class SlowSidecarMethod:
        @staticmethod
        async def remote(_task_ids):
            await asyncio.Event().wait()

    class SlowSidecar:
        get_stage_progress_snapshots = SlowSidecarMethod()

    snapshots = await JobService(tsm, stage_progress_manager=SlowSidecar())._live_stage_progress_snapshots(
        {"task-1": {"state": "SERIALIZING"}}
    )

    assert snapshots == {}
    assert tsm.stage_snapshot_bulk_reads == 1


@pytest.mark.asyncio
async def test_paginated_task_list_reads_only_the_ordered_durable_window() -> None:
    from core.models.catalog import DocumentStatus

    jobs = []
    for index in range(120):
        created_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=index)
        jobs.append(
            _job(
                id=f"task-{index:03}",
                status=DocumentStatus.COMPLETED,
                created_at=created_at,
                completed_at=created_at + timedelta(seconds=30),
                partition=f"partition-{index % 3}",
            )
        )
    repo = FakeJobRepo(jobs)

    result = await JobService(FakeTSM(), job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        limit=10,
        offset=50,
    )

    assert [task["task_id"] for task in result["tasks"]] == [f"task-{index:03}" for index in range(69, 59, -1)]
    assert repo.page_requests == [(50, 10, [])]
    assert result["pagination"] == {
        "offset": 50,
        "limit": 10,
        "total": 120,
        "has_more": True,
        "available_partitions": ["partition-0", "partition-1", "partition-2"],
    }


@pytest.mark.asyncio
async def test_paginated_task_list_compensates_for_actor_held_rows() -> None:
    from core.models.catalog import TASK_CREATED_AT_METADATA_KEY, DocumentStatus

    jobs = []
    for index in range(10):
        created_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=index)
        jobs.append(
            _job(
                id=f"task-{index:02}",
                status=DocumentStatus.COMPLETED,
                created_at=created_at,
                completed_at=created_at + timedelta(seconds=30),
                partition="durable-partition",
            )
        )
    actor_created_at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    tsm = FakeTSM(
        info={
            "actor-only": {
                "state": "COMPLETED",
                "details": {
                    "partition": "actor-partition",
                    "metadata": {TASK_CREATED_AT_METADATA_KEY: actor_created_at.isoformat()},
                },
                "user": 7,
            }
        }
    )
    repo = FakeJobRepo(jobs)

    result = await JobService(tsm, job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        limit=3,
        offset=4,
    )

    assert [task["task_id"] for task in result["tasks"]] == ["task-05", "task-04", "task-03"]
    assert repo.page_requests == [(4, 3, ["actor-only"])]
    assert result["pagination"]["total"] == 11
    assert result["pagination"]["available_partitions"] == ["actor-partition", "durable-partition"]


@pytest.mark.asyncio
async def test_paginated_task_metadata_reconciles_actor_terminal_state() -> None:
    from core.models.catalog import DocumentStatus

    task_id = "actor-terminal"
    job = _job(id=task_id, status=DocumentStatus.SERIALIZING, completed_at=None)
    tsm = FakeTSM(
        info={task_id: {"state": "FAILED", "details": {}, "user": 7}},
    )
    repo = FakeJobRepo([job])

    result = await JobService(tsm, job_repo=repo).list_tasks(
        is_admin=True,
        user_id=7,
        task_status="active",
        limit=10,
        offset=0,
    )

    assert result["tasks"] == []
    assert result["pagination"]["total"] == 0
    assert result["pagination"]["available_partitions"] == []


@pytest.mark.asyncio
async def test_list_tasks_pagination_defaults_limit_and_rejects_invalid_values() -> None:
    from services.orchestrators.job_service import JobService

    service = JobService(FakeTSM(), job_repo=FakeJobRepo())
    defaulted = await service.list_tasks(is_admin=True, user_id=1, offset=0)

    assert defaulted["pagination"]["limit"] == 50
    with pytest.raises(ValueError, match="pagination"):
        await service.list_tasks(is_admin=True, user_id=1, limit=101)
    with pytest.raises(ValueError, match="pagination"):
        await service.list_tasks(is_admin=True, user_id=1, limit=50, offset=2**63)


@pytest.mark.asyncio
async def test_task_stage_history_reads_durable_events_without_live_tail() -> None:
    from core.models.stage_progress import DurableStageProgress, StageEventStatus
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    tracker.finish_stage()
    tracker.finish_task()
    live = tracker.snapshot()
    durable = DurableStageProgress(
        task_id="task-1",
        latest_sequence=2,
        snapshot_revision=3,
        phase=live.summary.phase,
        position_score=500,
        expected_final_sequence=2,
        persisted_through_sequence=1,
        has_open_invocation=False,
    )
    repo = FakeJobRepo(
        [_job(id="task-1", stage_tracking_version=1)],
        stage_summaries={"task-1": durable},
        stage_events={"task-1": [live.event_tail[0]]},
    )
    service = JobService(
        FakeTSM(info={"task-1": {"state": "COMPLETED", "details": {}}}, stage_snapshots={"task-1": live}), job_repo=repo
    )

    result = await service.get_task_stage_progress("task-1", limit=10)

    assert [event["status"] for event in result["events"]] == [StageEventStatus.STARTED.value]
    assert result["history_quality"] == "unknown"
    assert result["last_known_stage_id"] == "parse"
    assert result["is_currently_running"] is False
    assert result["persisted_through_sequence"] == 1
    assert service._tsm.stage_snapshot_reads == 0


@pytest.mark.asyncio
async def test_active_task_stage_history_uses_fresh_worker_snapshot_and_keeps_durable_events() -> None:
    from core.models.stage_progress import DurableStageProgress, StageEventStatus
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("task-1")
    started = tracker.start_stage("parse", 0)
    live = tracker.snapshot()
    durable = DurableStageProgress.model_validate({**live.summary.model_dump(), "persisted_through_sequence": 0})
    repo = FakeJobRepo(
        [_job(id="task-1", status="SERIALIZING", stage_tracking_version=1, completed_at=None)],
        stage_summaries={"task-1": durable},
        stage_events={"task-1": [started]},
    )
    tsm = FakeTSM(
        info={"task-1": {"state": "SERIALIZING", "details": {}}},
        stage_snapshots={"task-1": live},
    )

    result = await JobService(tsm, job_repo=repo).get_task_stage_progress("task-1", limit=10)

    assert result["freshness"] == "live"
    assert result["is_currently_running"] is True
    assert result["stage_id"] == "parse"
    assert result["elapsed_ms"] is not None
    assert [event["status"] for event in result["events"]] == [StageEventStatus.STARTED.value]
    assert tsm.stage_snapshot_bulk_reads == 1


@pytest.mark.asyncio
async def test_sidecar_provides_live_progress_when_legacy_task_state_manager_lacks_snapshot_api() -> None:
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("task-legacy")
    tracker.start_stage("contextualize", 3)
    live = tracker.snapshot()
    tsm = FakeTSM(info={"task-legacy": {"state": "SERIALIZING", "details": {}}})
    del tsm.get_stage_progress_snapshots

    class Sidecar:
        get_stage_progress_snapshots = _Remote(
            lambda task_ids: {"task-legacy": live} if "task-legacy" in task_ids else {}
        )

    service = JobService(tsm, stage_progress_manager=Sidecar())
    snapshots = await service._live_stage_progress_snapshots({"task-legacy": {"state": "SERIALIZING"}})

    assert snapshots["task-legacy"].summary.current_stage_id == "contextualize"


@pytest.mark.asyncio
async def test_older_live_stage_snapshot_does_not_regress_durable_revision() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("task-1")
    tracker.start_stage("parse", 0)
    live = tracker.snapshot()
    durable = DurableStageProgress.model_validate(
        {
            **live.summary.model_dump(),
            "snapshot_revision": live.summary.snapshot_revision + 1,
            "persisted_through_sequence": 0,
        }
    )
    repo = FakeJobRepo(
        [_job(id="task-1", status="SERIALIZING", stage_tracking_version=1, completed_at=None)],
        stage_summaries={"task-1": durable},
    )
    tsm = FakeTSM(
        info={"task-1": {"state": "SERIALIZING", "details": {}}},
        stage_snapshots={"task-1": live},
    )

    result = await JobService(tsm, job_repo=repo).get_task_stage_progress("task-1", limit=10)

    assert result["freshness"] == "durable"
    assert result["is_currently_running"] is False


@pytest.mark.parametrize("reader_state", ["missing", "failing"])
@pytest.mark.asyncio
async def test_task_stage_history_fails_unavailable_when_durable_reader_is_unavailable(reader_state: str) -> None:
    from core.utils.exceptions import ServiceUnavailableError
    from services.orchestrators.job_service import JobService

    repo = FakeJobRepo([_job(id="task-1", stage_tracking_version=1)])
    if reader_state == "missing":
        repo.get_stage_progress_events = None
    else:

        async def fail_read(*_args, **_kwargs):
            raise RuntimeError("database unavailable")

        repo.get_stage_progress_events = fail_read

    service = JobService(FakeTSM(), job_repo=repo)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_stage_progress("task-1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "STAGE_HISTORY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_task_stage_history_returns_none_only_after_both_sources_confirm_absence() -> None:
    service = JobService(FakeTSM(), job_repo=FakeJobRepo())

    assert await service.get_task_stage_progress("missing-task") is None


@pytest.mark.parametrize("read_failure", ["job", "actor"])
@pytest.mark.asyncio
async def test_task_stage_history_does_not_report_404_when_existence_read_fails(read_failure: str) -> None:
    from core.utils.exceptions import ServiceUnavailableError

    repo = FakeJobRepo(fail_get_job=read_failure == "job")
    tsm = FakeTSM()
    if read_failure == "actor":

        def fail_get_state(_task_id):
            raise RuntimeError("actor unavailable")

        tsm.get_state = _Remote(fail_get_state)
    service = JobService(tsm, job_repo=repo)

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_stage_progress("possibly-existing-task")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "STAGE_HISTORY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_task_stage_history_fails_unavailable_when_summary_read_fails() -> None:
    from core.utils.exceptions import ServiceUnavailableError

    service = JobService(FakeTSM(), job_repo=FakeJobRepo([_job(id="task-1")], fail_stage_summaries=True))

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_stage_progress("task-1")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "STAGE_HISTORY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_older_stage_history_page_does_not_set_current_stage_from_historical_event() -> None:
    from uuid import uuid4

    from core.models.stage_progress import StageEventStatus, StageProgressEvent, StageProgressEventCursor

    occurred_at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    old_chunk = StageProgressEvent(
        task_id="task-1",
        attempt=1,
        sequence=2,
        invocation_id=uuid4(),
        stage_id="chunk",
        stage_rank=1,
        status=StageEventStatus.STARTED,
        occurred_at=occurred_at + timedelta(seconds=30),
    )
    old_parse = StageProgressEvent(
        task_id="task-1",
        attempt=1,
        sequence=1,
        invocation_id=uuid4(),
        stage_id="parse",
        stage_rank=0,
        status=StageEventStatus.STARTED,
        occurred_at=occurred_at,
    )
    events = [
        StageProgressEvent(
            task_id="task-1",
            attempt=2,
            sequence=1,
            invocation_id=uuid4(),
            stage_id="embed",
            stage_rank=5,
            status=StageEventStatus.STARTED,
            occurred_at=occurred_at + timedelta(minutes=1),
        ),
        old_chunk,
        old_parse,
    ]
    repo = FakeJobRepo(
        [_job(id="task-1", stage_tracking_version=1)],
        stage_events={"task-1": events},
    )
    service = JobService(FakeTSM(), job_repo=repo)

    result = await service.get_task_stage_progress(
        "task-1",
        before=StageProgressEventCursor(attempt=2, sequence=1),
    )

    assert [event["stage_id"] for event in result["events"]] == ["chunk", "parse"]
    assert result.get("last_known_stage_id") is None


def test_confirmed_worker_loss_is_reported_as_an_interrupted_last_known_stage() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.orchestrators.job_service import _stage_progress_view
    from services.workers.stage_tracker import StageTracker

    tracker = StageTracker("task-1")
    tracker.start_stage("embed", 5)
    summary = tracker.summary().model_copy(
        update={
            "interrupted_invocation_id": tracker.summary().current_invocation_id,
            "known_loss": True,
        }
    )
    durable = DurableStageProgress.model_validate(
        {**summary.model_dump(), "persisted_through_sequence": summary.latest_sequence}
    )

    result = _stage_progress_view(durable, state="FAILED", tracking_version=1)

    assert result["history_quality"] == "incomplete"
    assert result["is_currently_running"] is False
    assert result["interrupted_invocation_id"] == str(tracker.summary().current_invocation_id)
    assert result["last_known_stage_id"] == "embed"


def test_durable_parser_substage_is_returned_as_last_observed_progress() -> None:
    from uuid import uuid4

    from core.models.stage_progress import DurableStageProgress, StageTaskPhase
    from services.orchestrators.job_service import _stage_progress_view

    durable = DurableStageProgress(
        task_id="task-1",
        phase=StageTaskPhase.STAGE,
        current_stage_id="parse",
        current_stage_rank=0,
        current_substage_id="parse.text",
        current_substage_label="Extracting page text",
        current_substage_progress=42,
        current_substage_total=578,
        current_invocation_id=uuid4(),
        has_open_invocation=True,
        persisted_through_sequence=1,
    )

    result = _stage_progress_view(durable, state="SERIALIZING", tracking_version=1)

    assert result["freshness"] == "durable"
    assert result["is_currently_running"] is False
    assert result["current_substage_label"] == "Extracting page text"
    assert result["current_substage_progress"] == 42
    assert result["current_substage_total"] == 578


def test_conflict_marker_without_summary_is_exposed_as_incomplete_history() -> None:
    from core.models.stage_progress import DurableStageProgress
    from services.orchestrators.job_service import _stage_progress_view

    result = _stage_progress_view(
        DurableStageProgress(task_id="task-1", known_conflict=True),
        state="FAILED",
        tracking_version=1,
    )

    assert result["history_quality"] == "incomplete"
    assert result["known_loss"] is False
    assert result["known_conflict"] is True


def test_terminal_open_invocation_is_not_complete_without_worker_loss_evidence() -> None:
    from uuid import uuid4

    from core.models.stage_progress import DurableStageProgress, StageTaskPhase
    from services.orchestrators.job_service import _stage_progress_view

    invocation_id = uuid4()
    durable = DurableStageProgress(
        task_id="task-1",
        attempt=1,
        latest_sequence=1,
        snapshot_revision=3,
        phase=StageTaskPhase.STAGE,
        current_stage_id="embed",
        current_stage_rank=5,
        position_score=5000,
        current_invocation_id=invocation_id,
        expected_final_sequence=1,
        has_open_invocation=True,
        persisted_through_sequence=1,
    )

    result = _stage_progress_view(durable, state="FAILED", tracking_version=1)

    assert result["history_quality"] == "unknown"
    assert result["is_currently_running"] is False
    assert result["last_known_stage_id"] == "embed"


def test_durable_open_invocation_is_not_called_currently_running() -> None:
    from uuid import uuid4

    from core.models.stage_progress import DurableStageProgress, StageTaskPhase
    from services.orchestrators.job_service import _stage_progress_view

    durable = DurableStageProgress(
        task_id="task-1",
        attempt=1,
        latest_sequence=1,
        snapshot_revision=1,
        phase=StageTaskPhase.STAGE,
        current_stage_id="embed",
        current_stage_rank=5,
        position_score=5000,
        current_invocation_id=uuid4(),
        has_open_invocation=True,
        persisted_through_sequence=1,
    )

    result = _stage_progress_view(durable, state="SERIALIZING", tracking_version=1)

    assert result["freshness"] == "durable"
    assert result["is_currently_running"] is False


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

    def __init__(
        self,
        jobs=None,
        *,
        counts=None,
        broken: bool = False,
        fail_get_jobs: bool = False,
        fail_metadata: bool = False,
        fail_page: bool = False,
        fail_get_job: bool = False,
        fail_stage_summaries: bool = False,
        fail_latest_stage_ids: bool = False,
        stage_summaries=None,
        stage_events=None,
    ):
        self._jobs = {job.id: job for job in (jobs or [])}
        self._counts = counts
        self._broken = broken
        self._fail_get_jobs = fail_get_jobs
        self._fail_metadata = fail_metadata
        self._fail_page = fail_page
        self._fail_get_job = fail_get_job
        self._fail_stage_summaries = fail_stage_summaries
        self._fail_latest_stage_ids = fail_latest_stage_ids
        self.listed_statuses = []
        self.calls: list[str] = []
        self.stage_summary_requests: list[tuple[str, ...]] = []
        self._stage_summaries = stage_summaries or {}
        self._stage_events = stage_events or {}
        self.page_requests = []

    @asynccontextmanager
    async def task_list_snapshot(self):
        yield self

    async def list_jobs(self, *, statuses=None, user_id=None, offset=0, limit=50):
        if self._broken:
            raise RuntimeError("jobs table is missing")
        self.listed_statuses.append(statuses)
        return [
            job
            for job in self._jobs.values()
            if (user_id is None or job.user_id == user_id) and (statuses is None or job.status.value in statuses)
        ]

    async def list_jobs_by_stage_order(
        self,
        *,
        statuses=None,
        user_id=None,
        exclude_task_ids=None,
        offset=0,
        limit=50,
    ):
        from services.orchestrators.job_service import _job_to_info, _stage_progress_view, _task_order_key

        if self._fail_page:
            raise RuntimeError("job page is temporarily unavailable")
        excluded = set(exclude_task_ids or [])
        self.page_requests.append((offset, limit, sorted(excluded)))
        ordered = []
        for job in self._jobs.values():
            if job.id in excluded or (user_id is not None and job.user_id != user_id):
                continue
            if statuses is not None and job.status.value not in statuses:
                continue
            info = _job_to_info(job)
            if job.id in self._stage_summaries:
                info["stage_progress"] = _stage_progress_view(
                    self._stage_summaries[job.id],
                    state=job.status.value,
                    tracking_version=job.stage_tracking_version,
                )
            ordered.append(((job.id, info), job))
        ordered.sort(key=lambda item: _task_order_key(item[0]))
        return [job for _, job in ordered[offset : offset + limit]]

    async def list_task_page_by_stage_order(
        self,
        *,
        statuses=None,
        user_id=None,
        actor_overlays,
        offset=0,
        limit=50,
    ):
        from services.orchestrators.job_service import _job_to_info, _stage_progress_view, _task_order_key

        if self._fail_page:
            raise RuntimeError("job page is temporarily unavailable")
        overlay_by_id = {task_id: (state, created_at) for task_id, state, created_at in actor_overlays}
        self.page_requests.append((offset, limit, sorted(overlay_by_id)))
        ordered = []
        for job in self._jobs.values():
            if job.id in overlay_by_id or (user_id is not None and job.user_id != user_id):
                continue
            if statuses is not None and job.status.value not in statuses:
                continue
            info = _job_to_info(job)
            if job.id in self._stage_summaries:
                info["stage_progress"] = _stage_progress_view(
                    self._stage_summaries[job.id],
                    state=job.status.value,
                    tracking_version=job.stage_tracking_version,
                )
            ordered.append(((job.id, info), job))
        for task_id, (state, created_at) in overlay_by_id.items():
            if statuses is not None and state not in statuses:
                continue
            info = {"state": state, "created_at": created_at, "details": {}}
            if task_id in self._stage_summaries:
                info["stage_progress"] = _stage_progress_view(
                    self._stage_summaries[task_id],
                    state=state,
                    tracking_version=None,
                )
            ordered.append(((task_id, info), self._jobs.get(task_id)))
        ordered.sort(key=lambda item: _task_order_key(item[0]))
        return [(task_id, job) for (task_id, _), job in ordered[offset : offset + limit]]

    async def get_job_list_metadata(self, *, statuses=None, user_id=None):
        self.calls.append("get_job_list_metadata")
        if self._fail_metadata:
            raise RuntimeError("job metadata is temporarily unavailable")
        matches = [
            job
            for job in self._jobs.values()
            if (user_id is None or job.user_id == user_id) and (statuses is None or job.status.value in statuses)
        ]
        counts = {}
        for job in matches:
            counts[job.partition] = counts.get(job.partition, 0) + 1
        return len(matches), counts

    async def get_job_list_metadata_with_actor_rows(self, *, statuses=None, user_id=None, actor_task_ids):
        self.calls.append("get_job_list_metadata_with_actor_rows")
        if self._fail_metadata:
            raise RuntimeError("job metadata is temporarily unavailable")
        scoped = [job for job in self._jobs.values() if user_id is None or job.user_id == user_id]
        matches = [job for job in scoped if statuses is None or job.status.value in statuses]
        counts = {}
        for job in matches:
            counts[job.partition] = counts.get(job.partition, 0) + 1
        actor_rows = {job.id: (job.status.value, job.partition) for job in scoped if job.id in actor_task_ids}
        return len(matches), counts, actor_rows

    async def get_job(self, job_id):
        if self._broken or self._fail_get_job:
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

    async def get_stage_progress_summaries(self, task_ids):
        self.calls.append("get_stage_progress_summaries")
        self.stage_summary_requests.append(tuple(task_ids))
        if self._fail_stage_summaries:
            raise RuntimeError("stage summaries are temporarily unavailable")
        return {task_id: self._stage_summaries[task_id] for task_id in task_ids if task_id in self._stage_summaries}

    async def get_latest_stage_ids(self, task_ids):
        self.calls.append("get_latest_stage_ids")
        if self._fail_latest_stage_ids:
            raise RuntimeError("latest stage IDs are temporarily unavailable")
        result = {}
        for task_id in task_ids:
            events = self._stage_events.get(task_id, [])
            if events:
                latest = max(events, key=lambda event: (event.attempt, event.sequence))
                result[task_id] = latest.stage_id
        return result

    async def get_stage_progress_events(self, task_id, *, before=None, limit=100):
        from core.models.stage_progress import StageProgressEventPage

        self.calls.append("get_stage_progress_events")
        events = self._stage_events.get(task_id, [])
        if before is not None:
            events = [event for event in events if (event.attempt, event.sequence) < (before.attempt, before.sequence)]
        has_more = len(events) > limit
        page = events[-limit:]
        cursor = None
        if has_more and page:
            from core.models.stage_progress import StageProgressEventCursor

            cursor = StageProgressEventCursor(attempt=page[0].attempt, sequence=page[0].sequence)
        return StageProgressEventPage(events=page, has_more=has_more, next_cursor=cursor)


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
async def test_paginated_task_list_fails_when_aggregate_metadata_is_unavailable() -> None:
    from core.utils.exceptions import ServiceUnavailableError

    service = JobService(FakeTSM(), job_repo=FakeJobRepo([_job()], fail_metadata=True))

    with pytest.raises(ServiceUnavailableError, match="pagination metadata") as exc_info:
        await service.list_tasks(is_admin=True, user_id=7, limit=1, offset=0)

    assert exc_info.value.code == "TASK_LIST_PAGINATION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_paginated_task_list_uses_compact_actor_reconciliation_rows() -> None:
    actor_info = {"actor-only": {"state": "QUEUED", "details": {}, "user": 7}}
    repo = FakeJobRepo([_job()], fail_get_jobs=True)
    service = JobService(FakeTSM(info=actor_info), job_repo=repo)

    result = await service.list_tasks(is_admin=True, user_id=7, limit=1, offset=0)

    assert result["tasks"][0]["task_id"] == "actor-only"
    assert result["pagination"]["total"] == 2
    assert "get_jobs" not in repo.calls


@pytest.mark.asyncio
async def test_paginated_task_list_fails_when_durable_page_is_unavailable() -> None:
    from core.utils.exceptions import ServiceUnavailableError

    service = JobService(FakeTSM(), job_repo=FakeJobRepo([_job()], fail_page=True))

    with pytest.raises(ServiceUnavailableError, match="pagination data") as exc_info:
        await service.list_tasks(is_admin=True, user_id=7, limit=1, offset=0)

    assert exc_info.value.code == "TASK_LIST_PAGINATION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_paginated_task_list_rejects_repositories_without_ordered_paging() -> None:
    from core.utils.exceptions import ServiceUnavailableError

    class LegacyJobRepo:
        list_calls = 0

        async def list_jobs(self, **_kwargs):
            self.list_calls += 1
            return [_job()]

    repo = LegacyJobRepo()
    service = JobService(FakeTSM(), job_repo=repo)

    with pytest.raises(ServiceUnavailableError, match="not supported") as exc_info:
        await service.list_tasks(is_admin=True, user_id=7, limit=1, offset=0)

    assert exc_info.value.code == "TASK_LIST_PAGINATION_UNAVAILABLE"
    assert repo.list_calls == 0


@pytest.mark.asyncio
async def test_paginated_task_list_fails_when_stage_ordering_metadata_is_unavailable() -> None:
    from core.models.catalog import DocumentStatus
    from core.utils.exceptions import ServiceUnavailableError

    job = _job(status=DocumentStatus.SERIALIZING, completed_at=None)
    service = JobService(FakeTSM(), job_repo=FakeJobRepo([job], fail_stage_summaries=True))

    with pytest.raises(ServiceUnavailableError, match="ordering metadata") as exc_info:
        await service.list_tasks(is_admin=True, user_id=7, limit=1, offset=0)

    assert exc_info.value.code == "TASK_LIST_PAGINATION_UNAVAILABLE"


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


@pytest.mark.asyncio
async def test_get_task_details_fails_unavailable_when_database_and_actor_cannot_confirm_task():
    from core.utils.exceptions import ServiceUnavailableError

    service = JobService(FakeTSM(), job_repo=FakeJobRepo(broken=True))

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_details("possibly-existing-task")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "TASK_DETAILS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_get_task_details_maps_actor_timeout_for_missing_durable_row_to_unavailable() -> None:
    from core.utils.exceptions import ServiceUnavailableError

    def timeout(_task_id):
        raise TimeoutError("actor timed out")

    tsm = FakeTSM()
    tsm.get_details = _Remote(timeout)
    service = JobService(tsm, job_repo=FakeJobRepo())

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_details("possibly-existing-task")

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "TASK_DETAILS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_get_task_details_requires_owner_identity_when_durable_read_fails():
    from core.utils.exceptions import ServiceUnavailableError

    info = {"t-live": {"state": "QUEUED", "details": {"partition": "p"}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo(broken=True))

    with pytest.raises(ServiceUnavailableError) as exc_info:
        await service.get_task_details("t-live", require_owner_identity=True)

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "TASK_DETAILS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_get_task_details_allows_owner_identity_fallback_when_durable_read_fails() -> None:
    info = {"t-live": {"state": "QUEUED", "details": {"user_id": 7}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo(broken=True))

    assert await service.get_task_details("t-live", require_owner_identity=True) == {"user_id": 7}


@pytest.mark.asyncio
async def test_get_task_details_allows_admin_fallback_without_owner_identity():
    info = {"t-live": {"state": "QUEUED", "details": {"partition": "p"}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo(broken=True))

    assert await service.get_task_details("t-live", require_owner_identity=False) == {"partition": "p"}


@pytest.mark.asyncio
async def test_get_task_details_uses_available_actor_details_when_database_is_unavailable():
    info = {"t-live": {"state": "QUEUED", "details": {"user_id": 7}, "user": 7}}
    service = JobService(FakeTSM(info=info), job_repo=FakeJobRepo(broken=True))

    assert await service.get_task_details("t-live") == {"user_id": 7}
