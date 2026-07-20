"""Bootstrap must register restart factories for the lazy parser pools.

``actor_creation_map`` is process-local and read by the admin restart
endpoint (``POST /cluster/{actor}/restart``) from the API process. Parser
pools are created on first use — possibly inside another Ray worker
process — so bootstrap has to register their restart factories eagerly,
without creating the actors, or the endpoint 404s for every parser pool.
"""

from __future__ import annotations

import importlib

import pytest
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
    for creator in ("init_llm_semaphore", "init_vlm_semaphore", "init_audio_semaphore", "get_task_state_manager"):
        monkeypatch.setattr(bootstrap, creator, lambda: None)

    bootstrap.initialize_worker_bootstrap(Settings())

    assert set(PARSER_POOL_ACTORS) <= set(bootstrap.actor_creation_map)


def test_task_state_manager_uses_versioned_actor_name(monkeypatch):
    from services.workers.task_state import TaskStateManager

    calls = _capture_actor_creations(monkeypatch)

    assert bootstrap.get_task_state_manager() == "actor-handle"

    ((name, cls, options),) = calls
    assert name == "TaskStateManagerV2"
    assert cls is TaskStateManager
    assert options == {"lifetime": "detached"}


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
