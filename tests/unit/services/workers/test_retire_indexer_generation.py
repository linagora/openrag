from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def test_generation_actor_names_do_not_mix_protocols() -> None:
    from services.workers.retire_indexer_generation import _generation_actor_names

    actors = [
        {"name": "IndexerWorker-0", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-0", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-1", "state": "ALIVE"},
        {"name": "IndexerWorker-v3-0", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-2", "state": "DEAD"},
    ]

    assert _generation_actor_names(actors, "legacy") == ("IndexerPoolDispatcher", ["IndexerWorker-0"])
    assert _generation_actor_names(actors, "v2") == (
        "IndexerPoolDispatcher-v2",
        ["IndexerWorker-v2-0", "IndexerWorker-v2-1"],
    )


def test_legacy_retirement_requires_explicit_idle_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = object()
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)

    with pytest.raises(RuntimeError, match="confirm-legacy-idle"):
        module.retire_generation(
            "legacy",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )


def test_protocol_generation_drains_before_removal(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    begin_drain = Mock()
    status = Mock()
    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=begin_drain),
        status=SimpleNamespace(remote=status),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    actors = [
        {"name": "IndexerPoolDispatcher-v2", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-0", "state": "ALIVE"},
    ]
    rpc_results = iter(
        [
            {"inflight_jobs": 1, "worker_names": ["IndexerWorker-v2-0"]},
            {"inflight_jobs": 0, "worker_names": ["IndexerWorker-v2-0"]},
        ]
    )
    killed: list[str] = []

    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: actors)
    monkeypatch.setattr(module, "_get_actor", lambda name, _namespace: dispatcher if "Dispatcher" in name else name)
    rpc = AsyncMock(side_effect=lambda *_args: next(rpc_results))
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(module, "_kill_actor", lambda name, _namespace: not killed.append(name))

    assert module.retire_generation(
        "v2",
        namespace="openrag",
        timeout=1,
        poll_interval=0,
        confirm_legacy_idle=False,
    ) == ["IndexerPoolDispatcher-v2", "IndexerWorker-v2-0"]
    assert killed == ["IndexerPoolDispatcher-v2", "IndexerWorker-v2-0"]
    begin_drain.assert_called_once()
    assert isinstance(begin_drain.call_args.args[0], str)
    dispatcher.abort_drain.remote.assert_not_called()
    status.assert_called_once_with()
    assert [call.args[2] for call in rpc.await_args_list] == [
        "Begin draining indexer generation v2",
        "Check indexer generation v2 drain status",
    ]


def test_missing_dispatcher_requires_idle_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    actors = [
        {"name": "IndexerWorker-v2-0", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-1", "state": "ALIVE"},
    ]
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: actors)
    monkeypatch.setattr(module, "_get_actor", lambda *_args: None)
    kill = Mock()
    monkeypatch.setattr(module, "_kill_actor", kill)

    with pytest.raises(RuntimeError, match="confirm-workers-idle"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )
    kill.assert_not_called()


def test_missing_dispatcher_removes_confirmed_idle_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    actors = [
        {"name": "IndexerWorker-v2-0", "state": "ALIVE"},
        {"name": "IndexerWorker-v2-1", "state": "ALIVE"},
    ]
    killed: list[str] = []

    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: actors)
    monkeypatch.setattr(module, "_get_actor", lambda *_args: None)
    monkeypatch.setattr(module, "_kill_actor", lambda name, _namespace: not killed.append(name))

    assert module.retire_generation(
        "v2",
        namespace="openrag",
        timeout=1,
        poll_interval=0,
        confirm_legacy_idle=False,
        confirm_workers_idle=True,
    ) == ["IndexerWorker-v2-0", "IndexerWorker-v2-1"]
    assert killed == ["IndexerWorker-v2-0", "IndexerWorker-v2-1"]


def test_begin_drain_rpc_timeout_keeps_generation_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    rpc = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    kill = Mock()
    monkeypatch.setattr(module, "_kill_actor", kill)

    with pytest.raises(TimeoutError, match="actors were kept"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )
    kill.assert_not_called()
    dispatcher.abort_drain.remote.assert_not_called()


def test_status_rpc_timeout_keeps_generation_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        status=SimpleNamespace(remote=Mock()),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    rpc = AsyncMock(side_effect=[{"inflight_jobs": 1}, TimeoutError(), {"accepting_tasks": True}])
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    kill = Mock()
    monkeypatch.setattr(module, "_kill_actor", kill)

    with pytest.raises(TimeoutError, match="actors were kept"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )
    kill.assert_not_called()
    assert rpc.await_count == 3
    dispatcher.abort_drain.remote.assert_called_once()


def test_status_failure_restores_acceptance_and_reraises_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        status=SimpleNamespace(remote=Mock()),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    rpc = AsyncMock(side_effect=[{"inflight_jobs": 1}, RuntimeError("status failed"), {"accepting_tasks": True}])
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    kill = Mock()
    monkeypatch.setattr(module, "_kill_actor", kill)

    with pytest.raises(RuntimeError, match="status failed"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )

    kill.assert_not_called()
    dispatcher.abort_drain.remote.assert_called_once()


def test_actor_kill_failure_does_not_restore_a_completed_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", AsyncMock(return_value={"inflight_jobs": 0}))
    monkeypatch.setattr(module, "_kill_actor", Mock(side_effect=RuntimeError("kill failed")))

    with pytest.raises(RuntimeError, match="kill failed"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )

    dispatcher.abort_drain.remote.assert_not_called()


def test_recovery_failure_keeps_the_retirement_error_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        status=SimpleNamespace(remote=Mock()),
        abort_drain=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    rpc = AsyncMock(side_effect=[{"inflight_jobs": 1}, TimeoutError(), RuntimeError("abort failed")])
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    logger = Mock()
    monkeypatch.setattr(module, "logger", logger)

    with pytest.raises(TimeoutError, match="actors were kept"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )

    logger.error.assert_called_once()
    assert logger.error.call_args.kwargs["error"] == "abort failed"


def test_timeout_restores_acceptance_on_the_same_live_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module
    from services.workers.indexer_pool import IndexerPool

    submitted: list[str] = []

    def process_file(**kwargs):
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        submitted.append(kwargs["task_id"])
        return future

    actor_class = IndexerPool.__ray_metadata__.modified_class
    pool = actor_class.__new__(actor_class)
    pool._workers = [SimpleNamespace(process_file=SimpleNamespace(remote=process_file))]
    pool._worker_names = ["IndexerWorker-v2-0"]
    pool._inflight = [1]
    pool._accepting_tasks = True
    pool._drain_operation_id = None
    pool._release_tasks = set()
    pool._claim_store = None
    pool._claim_store_lock = asyncio.Lock()

    status_ref = object()
    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=pool.begin_drain),
        status=SimpleNamespace(remote=lambda: status_ref),
        abort_drain=SimpleNamespace(remote=pool.abort_drain),
    )

    async def rpc(future, *_args):
        if future is status_ref:
            raise TimeoutError
        return await future

    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    monkeypatch.setattr(module, "call_ray_actor_with_timeout", rpc)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    kill = Mock()
    monkeypatch.setattr(module, "_kill_actor", kill)

    with pytest.raises(TimeoutError, match="actors were kept"):
        module.retire_generation(
            "v2",
            namespace="openrag",
            timeout=1,
            poll_interval=0,
            confirm_legacy_idle=False,
        )

    async def submit_after_timeout() -> None:
        assert (await pool.status())["accepting_tasks"] is True
        await pool.submit(task_id="accepted-after-timeout")
        await asyncio.gather(*pool._release_tasks)

    asyncio.run(submit_after_timeout())
    kill.assert_not_called()
    assert submitted == ["accepted-after-timeout"]
