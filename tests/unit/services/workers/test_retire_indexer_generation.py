from __future__ import annotations

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
    begin_drain.assert_called_once_with()
    status.assert_called_once_with()
    assert [call.args[2] for call in rpc.await_args_list] == [
        "Begin draining indexer generation v2",
        "Check indexer generation v2 drain status",
    ]


def test_missing_dispatcher_still_removes_discovered_workers(monkeypatch: pytest.MonkeyPatch) -> None:
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
    ) == ["IndexerWorker-v2-0", "IndexerWorker-v2-1"]
    assert killed == ["IndexerWorker-v2-0", "IndexerWorker-v2-1"]


def test_begin_drain_rpc_timeout_keeps_generation_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(begin_drain=SimpleNamespace(remote=Mock()))
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


def test_status_rpc_timeout_keeps_generation_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(
        begin_drain=SimpleNamespace(remote=Mock()),
        status=SimpleNamespace(remote=Mock()),
    )
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    rpc = AsyncMock(side_effect=[{"inflight_jobs": 1}, TimeoutError()])
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
    assert rpc.await_count == 2
