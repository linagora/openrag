"""Bootstrap must register restart factories for the lazy parser pools.

``actor_creation_map`` is process-local and read by the admin restart
endpoint (``POST /cluster/{actor}/restart``) from the API process. Parser
pools are created on first use — possibly inside another Ray worker
process — so bootstrap has to register their restart factories eagerly,
without creating the actors, or the endpoint 404s for every parser pool.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import ray
import services.workers.bootstrap as bootstrap

PARSER_POOL_ACTORS = ("MarkerPool", "DoclingPool", "WhisperPool", "WhisperActor")


def _capture_actor_creations(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    def fake_get_or_create_actor(name, cls, **options):
        calls.append((name, cls, options))
        return "actor-handle"

    monkeypatch.setattr(bootstrap, "actor_creation_map", {})
    monkeypatch.setattr(bootstrap, "get_or_create_actor", fake_get_or_create_actor)
    return calls


def test_registers_all_parser_pools_without_creating_actors(monkeypatch):
    calls = _capture_actor_creations(monkeypatch)

    bootstrap.register_parser_pool_restart_factories()

    assert set(bootstrap.actor_creation_map) == set(PARSER_POOL_ACTORS)
    assert calls == []


def test_initialize_worker_bootstrap_registers_parser_pool_factories(monkeypatch):
    from core.config.root import Settings

    monkeypatch.setattr(bootstrap, "actor_creation_map", {})
    monkeypatch.setattr(bootstrap, "_settings", None)
    for creator in (
        "init_llm_semaphore",
        "init_vlm_semaphore",
        "init_audio_semaphore",
        "get_task_state_manager",
        "get_task_completion_tracker",
    ):
        monkeypatch.setattr(bootstrap, creator, lambda: None)

    bootstrap.initialize_worker_bootstrap(Settings())

    assert set(PARSER_POOL_ACTORS) <= set(bootstrap.actor_creation_map)


def test_task_state_manager_restarts_without_retrying_mutations(monkeypatch):
    calls = []
    actor = SimpleNamespace(supports_in_place_restart=SimpleNamespace())

    def fake_get_or_create_actor(name, cls, **options):
        calls.append((name, cls, options))
        return actor

    monkeypatch.setattr(bootstrap, "actor_creation_map", {})
    monkeypatch.setattr(bootstrap, "get_or_create_actor", fake_get_or_create_actor)

    assert bootstrap.get_task_state_manager() is actor

    ((name, _cls, options),) = calls
    assert name == "TaskStateManager"
    assert options == {
        "lifetime": "detached",
        "max_restarts": -1,
        "max_task_retries": 0,
    }


def test_legacy_task_state_manager_is_replaced_before_handle_is_returned(monkeypatch):
    legacy = SimpleNamespace()
    replacement = SimpleNamespace(supports_in_place_restart=SimpleNamespace())
    get_or_create = Mock(side_effect=[legacy, replacement])
    kill = Mock()
    monkeypatch.setattr(bootstrap, "actor_creation_map", {})
    monkeypatch.setattr(bootstrap, "get_or_create_actor", get_or_create)
    monkeypatch.setattr(ray, "kill", kill)
    monkeypatch.setattr(ray, "get_actor", Mock(side_effect=ValueError("actor removed")))

    assert bootstrap.get_task_state_manager() is replacement

    kill.assert_called_once_with(legacy, no_restart=True)
    assert get_or_create.call_count == 2


def test_task_completion_tracker_is_detached_and_starts_recovery(monkeypatch):
    calls = []
    tracker = Mock()
    monkeypatch.setattr(bootstrap, "actor_creation_map", {})

    def fake_get_or_create_actor(name, cls, **options):
        calls.append((name, cls, options))
        return tracker

    monkeypatch.setattr(bootstrap, "get_or_create_actor", fake_get_or_create_actor)

    assert bootstrap.get_task_completion_tracker() is tracker

    ((name, _cls, options),) = calls
    assert name == "TaskCompletionTracker"
    assert options == {
        "namespace": "openrag",
        "remote_args": ("openrag",),
        "lifetime": "detached",
    }
    tracker.recover.remote.assert_called_once_with()
    assert bootstrap.actor_creation_map["TaskCompletionTracker"]() is tracker


@pytest.mark.parametrize(
    ("actor_name", "module_name"),
    [
        ("MarkerPool", "services.workers.parsers.marker_workers"),
        ("DoclingPool", "services.workers.parsers.docling_workers"),
        ("WhisperPool", "services.workers.parsers.whisper_workers"),
    ],
)
def test_pool_factory_creates_the_named_pool(monkeypatch, actor_name, module_name):
    calls = _capture_actor_creations(monkeypatch)
    bootstrap.register_parser_pool_restart_factories()

    assert bootstrap.actor_creation_map[actor_name]() == "actor-handle"

    pool_cls = getattr(importlib.import_module(module_name), actor_name)
    assert calls == [(actor_name, pool_cls, {"lifetime": "detached"})]


def test_whisper_actor_factory_passes_actor_options(monkeypatch):
    from core.config.root import Settings
    from services.workers.parsers.whisper_workers import WhisperActor

    calls = _capture_actor_creations(monkeypatch)
    monkeypatch.setattr(bootstrap, "_settings", Settings())
    bootstrap.register_parser_pool_restart_factories()

    assert bootstrap.actor_creation_map["WhisperActor"]() == "actor-handle"

    ((name, cls, options),) = calls
    assert name == "WhisperActor"
    assert cls is WhisperActor
    assert options["lifetime"] == "detached"
    # The per-actor Ray options (GPU reservation, restarts, concurrency) must
    # be recomputed at restart time, matching detect_language_via_actor.
    assert {"num_gpus", "max_restarts", "max_concurrency"} <= options.keys()
