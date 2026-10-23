from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

import services.workers.task_cancellation as task_cancellation


class _RemoteMethod:
    def __init__(self, result: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result

    async def remote(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.result


class _TaskStateManager:
    def __init__(self) -> None:
        self.set_cancelled_if_active = _RemoteMethod(False)
        self.set_state = _RemoteMethod(None)


def test_cancel_refs_does_not_overwrite_a_task_that_settled_before_cancellation(
    monkeypatch,
) -> None:
    manager = _TaskStateManager()
    cancelled_refs: list[Any] = []

    monkeypatch.setattr(task_cancellation.ray, "cancel", lambda ref, recursive: cancelled_refs.append(ref))

    async def wait_for_task_to_settle(*args: Any, **kwargs: Any) -> None:
        return None

    async def call_actor_method_with_timeout(*, submit, **kwargs: Any) -> Any:
        return await submit()

    monkeypatch.setattr(task_cancellation, "_wait_for_task_to_settle", wait_for_task_to_settle)
    monkeypatch.setattr(
        task_cancellation,
        "call_ray_actor_method_with_timeout",
        call_actor_method_with_timeout,
    )

    result = asyncio.run(
        task_cancellation._cancel_refs(
            manager,
            {"task-1": "ref-1"},
            deadline=monotonic() + 1.0,
            partition="tenant-a",
            file_id="file-1",
        )
    )

    assert result[0] == 1
    assert cancelled_refs == ["ref-1"]
    assert manager.set_cancelled_if_active.calls == [("task-1",)]
    assert manager.set_state.calls == []
