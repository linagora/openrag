from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

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
    ray_results = iter(
        [
            {"inflight_jobs": 1, "worker_names": ["IndexerWorker-v2-0"]},
            {"inflight_jobs": 0, "worker_names": ["IndexerWorker-v2-0"]},
        ]
    )
    killed: list[str] = []

    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: actors)
    monkeypatch.setattr(module, "_get_actor", lambda name, _namespace: dispatcher if "Dispatcher" in name else name)
    monkeypatch.setattr(module.ray, "get", lambda _ref: next(ray_results))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
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


def test_timeout_keeps_generation_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.retire_indexer_generation as module

    dispatcher = SimpleNamespace(begin_drain=SimpleNamespace(remote=Mock()))
    monkeypatch.setattr("ray.util.state.list_actors", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_get_actor", lambda *_args: dispatcher)
    monkeypatch.setattr(module.ray, "get", lambda _ref: {"inflight_jobs": 1})
    monkeypatch.setattr(module.time, "monotonic", Mock(side_effect=[0.0, 2.0]))
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
