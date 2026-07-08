from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from core.config.root import Settings
from di.workers import ensure_worker_bootstrap, list_ray_actors


def _fake_actor(name: str, namespace: str):
    return SimpleNamespace(
        actor_id=f"id-{name}",
        name=name,
        class_name="SomeActor",
        state="ALIVE",
        ray_namespace=namespace,
    )


def test_list_ray_actors_filters_out_non_openrag_namespace() -> None:
    """Ray-internal job actors live outside the ``openrag`` namespace and must be
    hidden from the admin system view (they're dead once their job ends and
    can't be restarted)."""
    actors = [
        _fake_actor("Indexer", "openrag"),
        _fake_actor("WhisperPool", "openrag"),
        _fake_actor("_ray_internal_job_actor_raysubmit_abc", "_ray_internal_job"),
        _fake_actor("SomethingElse", "default"),
    ]
    list_actors_mock = Mock(return_value=actors)
    module = ModuleType("ray.util.state")
    module.list_actors = list_actors_mock
    previous_module = sys.modules.get("ray.util.state")
    sys.modules["ray.util.state"] = module

    try:
        result = list_ray_actors()
    finally:
        if previous_module is None:
            sys.modules.pop("ray.util.state", None)
        else:
            sys.modules["ray.util.state"] = previous_module

    # Namespace is filtered server-side so Ray's default limit=100 applies within
    # the openrag namespace rather than across all namespaces.
    list_actors_mock.assert_called_once_with(filters=[("ray_namespace", "=", "openrag")])
    # Defense-in-depth: even if the mock ignores the filter, non-openrag actors
    # are dropped by the in-comprehension guard.
    assert {a["name"] for a in result} == {"Indexer", "WhisperPool"}
    assert all(a["namespace"] == "openrag" for a in result)


def test_ensure_worker_bootstrap_initializes_explicitly() -> None:
    """Startup calls the worker bootstrap function instead of relying on import side effects."""
    module = ModuleType("services.workers.bootstrap")
    module.initialize_worker_bootstrap = Mock()
    previous_module = sys.modules.get("services.workers.bootstrap")
    sys.modules["services.workers.bootstrap"] = module
    settings = Settings()

    try:
        ensure_worker_bootstrap(settings)
    finally:
        if previous_module is None:
            sys.modules.pop("services.workers.bootstrap", None)
        else:
            sys.modules["services.workers.bootstrap"] = previous_module

    module.initialize_worker_bootstrap.assert_called_once_with(settings)


def test_worker_bootstrap_import_has_no_actor_side_effects() -> None:
    """Importing worker bootstrap does not create detached actors."""
    sys.modules.pop("services.workers.bootstrap", None)

    module = import_module("services.workers.bootstrap")

    assert module.actor_creation_map == {}
    assert not hasattr(module, "task_state_manager")
    assert not hasattr(module, "serializer")
