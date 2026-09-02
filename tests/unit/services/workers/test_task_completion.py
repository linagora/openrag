from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _remote_mock(return_value: Any = None) -> MagicMock:
    method = MagicMock()
    method.remote = AsyncMock(return_value=return_value)
    return method


def _task_state_manager(
    *,
    all_info: dict[str, dict] | None = None,
    object_ref: Any = None,
    state: str | None = None,
) -> MagicMock:
    tsm = MagicMock()
    tsm.get_all_info = _remote_mock(all_info or {})
    tsm.get_object_ref = _remote_mock(object_ref)
    tsm.get_state = _remote_mock(state)
    tsm.get_details = _remote_mock()
    tsm.set_details = _remote_mock()
    tsm.finish_cancellation = _remote_mock()
    tsm.expire_refless_task_if_stale = _remote_mock(False)
    tsm.has_unsettled_cancelled_worker = _remote_mock(False)
    return tsm


@pytest.mark.asyncio
async def test_tracker_records_completion_after_worker_settles() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    ref = asyncio.get_running_loop().create_future()
    tsm = _task_state_manager()
    tsm.get_details.remote.return_value = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"filename": "report.pdf", "_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion._utc_now_iso", return_value="2026-07-20T08:01:05+00:00"),
    ):
        tracker = TaskCompletionTracker()
        watch = asyncio.create_task(tracker.track("task-1", {"ref": ref}))
        await asyncio.sleep(0)
        ref.set_result(None)
        await watch

    tsm.set_details.remote.assert_awaited_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={
            "filename": "report.pdf",
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
            "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
        },
        user_id=42,
    )
    tsm.finish_cancellation.remote.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_tracker_recovers_active_task_after_restart() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    ref = asyncio.get_running_loop().create_future()
    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(
        all_info={"task-1": {"state": "SERIALIZING", "details": details}},
        object_ref={"ref": ref},
    )
    tsm.get_details.remote.return_value = details
    tracker_handle = MagicMock()

    def get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        return tsm if name == "TaskStateManager" else tracker_handle

    with patch("services.workers.task_completion.ray.get_actor", side_effect=get_actor):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tsm.get_object_ref.remote.assert_awaited_once_with("task-1")
    tracker_handle.track.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.parametrize("bare_ref", [False, True], ids=["wrapped-ref", "bare-ref"])
@pytest.mark.asyncio
async def test_tracker_keeps_recovered_cancellation_tracked_until_worker_settles(bare_ref: bool) -> None:
    from services.workers.task_completion import TaskCompletionTracker

    ref = asyncio.get_running_loop().create_future()
    details = {
        "metadata": {
            "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
        }
    }
    tsm = _task_state_manager(
        all_info={"task-1": {"state": "CANCELLED", "details": details}},
        object_ref=ref if bare_ref else {"ref": ref},
    )
    tracker_handle = MagicMock()

    def get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        return tsm if name == "TaskStateManager" else tracker_handle

    with patch("services.workers.task_completion.ray.get_actor", side_effect=get_actor):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tsm.get_object_ref.remote.assert_awaited_once_with("task-1")
    tracker_handle.track.remote.assert_called_once_with("task-1", {"ref": ref})


@pytest.mark.asyncio
async def test_tracker_keeps_submitted_refless_cancellation_pending() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(
        all_info={
            "task-1": {
                "state": "CANCELLED",
                "details": details,
                "worker_submitted": True,
            }
        },
        object_ref=None,
    )
    tracker_handle = MagicMock()

    def get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        return tsm if name == "TaskStateManager" else tracker_handle

    with patch("services.workers.task_completion.ray.get_actor", side_effect=get_actor):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tracker_handle.recover_refless.remote.assert_called_once_with("task-1", preserve_cancelled_submission=True)
    tsm.set_details.remote.assert_not_called()


@pytest.mark.asyncio
async def test_tracker_retries_active_task_without_stored_ref_after_restart() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(
        all_info={"task-1": {"state": "SERIALIZING", "details": details}},
        object_ref=None,
    )
    tracker_handle = MagicMock()

    def get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        return tsm if name == "TaskStateManager" else tracker_handle

    with patch("services.workers.task_completion.ray.get_actor", side_effect=get_actor):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tracker_handle.recover_refless.remote.assert_called_once_with("task-1")


@pytest.mark.asyncio
async def test_tracker_records_recovered_refless_task_when_it_reaches_terminal_state() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(object_ref=None)
    tsm.get_details.remote.return_value = details
    tsm.get_state.remote.side_effect = ["SERIALIZING", "COMPLETED"]

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion._utc_now_iso", return_value="2026-07-20T08:01:05+00:00"),
        patch("services.workers.task_completion.asyncio.sleep", AsyncMock()),
    ):
        tracker = TaskCompletionTracker()
        await tracker.recover_refless("task-1", poll_interval=0)

    tsm.set_details.remote.assert_awaited_once_with(
        "task-1",
        file_id="file-1",
        partition="tenant-a",
        metadata={
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
            "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
        },
        user_id=42,
    )


@pytest.mark.asyncio
async def test_tracker_waits_for_submitted_cancelled_worker_ref() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    ref = asyncio.get_running_loop().create_future()
    ref.set_result(None)
    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(state="CANCELLED")
    tsm.get_details.remote.return_value = details
    tsm.get_object_ref.remote.side_effect = [None, {"ref": ref}]
    tsm.has_unsettled_cancelled_worker.remote.return_value = True

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion._utc_now_iso", return_value="2026-07-20T08:01:05+00:00"),
        patch("services.workers.task_completion.asyncio.sleep", AsyncMock()),
    ):
        tracker = TaskCompletionTracker()
        await tracker.recover_refless(
            "task-1",
            poll_interval=0,
            preserve_cancelled_submission=True,
        )

    assert tsm.get_object_ref.remote.await_count == 2
    tsm.set_details.remote.assert_awaited_once()
    tsm.finish_cancellation.remote.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_tracker_finishes_cancelled_submission_after_pool_clears_fence() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(state="CANCELLED")
    tsm.get_details.remote.return_value = details
    tsm.has_unsettled_cancelled_worker.remote.side_effect = [True, False]

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion._utc_now_iso", return_value="2026-07-20T08:01:05+00:00"),
        patch("services.workers.task_completion.asyncio.sleep", AsyncMock()),
    ):
        tracker = TaskCompletionTracker()
        await tracker.recover_refless(
            "task-1",
            poll_interval=0,
            preserve_cancelled_submission=True,
        )

    assert tsm.has_unsettled_cancelled_worker.remote.await_count == 2
    tsm.set_details.remote.assert_awaited_once()
    tsm.finish_cancellation.remote.assert_awaited_once_with("task-1")


@pytest.mark.asyncio
async def test_tracker_expires_recovered_refless_task_after_registration_grace() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2000-01-01T00:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(object_ref=None)
    tsm.get_details.remote.return_value = details
    tsm.expire_refless_task_if_stale.remote.return_value = True

    with patch("services.workers.task_completion.ray.get_actor", return_value=tsm):
        tracker = TaskCompletionTracker()
        await tracker.recover_refless("task-1", poll_interval=0)

    tsm.expire_refless_task_if_stale.remote.assert_awaited_once_with("task-1")
    tsm.set_details.remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracker_bounds_refless_expiration_actor_call() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    tsm = _task_state_manager(object_ref=None)
    tsm.get_details.remote.return_value = {"metadata": {}}
    bounded_call = AsyncMock(side_effect=[{"metadata": {}}, TimeoutError])

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
    ):
        tracker = TaskCompletionTracker()
        tracker._call_task_state = bounded_call
        await tracker.recover_refless("task-1", poll_interval=0)

    assert "task-1" not in tracker._tracked_task_ids
    assert bounded_call.await_count == 2
    assert bounded_call.await_args.args[1] == "expire_refless_task_if_stale(task-1)"


@pytest.mark.asyncio
async def test_tracker_bounds_cancelled_worker_ref_lookup() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    tsm = _task_state_manager(all_info={"task-1": {"state": "CANCELLED"}})
    tracker_handle = MagicMock()

    def get_actor(name: str, namespace: str):
        assert namespace == "openrag"
        return tsm if name == "TaskStateManager" else tracker_handle

    with patch("services.workers.task_completion.ray.get_actor", side_effect=get_actor):
        tracker = TaskCompletionTracker()
        tracker._call_task_state = AsyncMock(side_effect=[{"task-1": {"state": "CANCELLED"}}, TimeoutError])
        await tracker.recover()

    assert tracker._call_task_state.await_count == 2
    assert tracker._call_task_state.await_args.args[1] == "get_object_ref(task-1) for cancellation recovery"


@pytest.mark.asyncio
async def test_tracker_bounds_cancellation_finalization_actor_call() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    tsm = _task_state_manager()
    bounded_call = AsyncMock(side_effect=TimeoutError)

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion.call_ray_actor_method_with_timeout", bounded_call),
    ):
        tracker = TaskCompletionTracker()
        await tracker._finish_cancellation("task-1")

    assert bounded_call.await_args.kwargs["timeout"] == 30.0
    assert bounded_call.await_args.kwargs["task_description"] == "finish_cancellation(task-1)"


@pytest.mark.asyncio
async def test_tracker_backfills_terminal_task_missed_during_api_restart() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "file_id": "file-1",
        "partition": "tenant-a",
        "metadata": {"_openrag_job_created_at": "2026-07-20T08:00:00+00:00"},
        "user_id": 42,
    }
    tsm = _task_state_manager(all_info={"task-1": {"state": "COMPLETED", "details": details}})
    tsm.get_details.remote.return_value = details

    with (
        patch("services.workers.task_completion.ray.get_actor", return_value=tsm),
        patch("services.workers.task_completion._utc_now_iso", return_value="2026-07-20T08:01:05+00:00"),
    ):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tsm.get_object_ref.remote.assert_not_called()
    tsm.set_details.remote.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracker_does_not_replace_existing_completion_time() -> None:
    from services.workers.task_completion import TaskCompletionTracker

    details = {
        "metadata": {
            "_openrag_job_created_at": "2026-07-20T08:00:00+00:00",
            "_openrag_job_finished_at": "2026-07-20T08:01:05+00:00",
        }
    }
    tsm = _task_state_manager(all_info={"task-1": {"state": "COMPLETED", "details": details}})

    with patch("services.workers.task_completion.ray.get_actor", return_value=tsm):
        tracker = TaskCompletionTracker()
        await tracker.recover()

    tsm.get_details.remote.assert_not_called()
    tsm.set_details.remote.assert_not_called()
