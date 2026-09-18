from __future__ import annotations

import asyncio
import sys
from importlib import import_module
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import di.workers
import pytest
import ray
from core.config.root import Settings
from di.workers import ensure_worker_bootstrap, list_ray_actors, restart_ray_actor
from ray.util.state.exception import RayStateApiException


def _fake_actor(name: str, namespace: str = "openrag", state: str = "ALIVE", actor_id: str | None = None):
    return SimpleNamespace(
        actor_id=actor_id or f"id-{name}",
        name=name,
        class_name="SomeActor",
        state=state,
        ray_namespace=namespace,
    )


def _list_ray_actors_over(monkeypatch, actors, *, honor_filters: bool = True, data_source_cap: int = 10_000):
    """Run ``list_ray_actors`` against a fake ``ray.util.state.list_actors``.

    With ``honor_filters`` the fake behaves like Ray 2.47's state API. The data
    source (GCS) reads records in the order ``actors`` lists them (its own order
    is arbitrary), applies only ``state =`` filters, and stops once
    ``data_source_cap`` records have matched; with ``raise_on_missing_output``
    a cut there raises. The API server then applies the remaining filters, sorts
    by actor id and keeps ``limit``, 100 by default. Without ``honor_filters``
    the fake returns everything regardless.
    """

    def matches(actor, filters):
        return all((getattr(actor, key) == value) == (predicate == "=") for key, predicate, value in filters)

    def list_actors(filters, limit=100, raise_on_missing_output=True):
        if not honor_filters:
            return list(actors)
        source_filters = [f for f in filters if f[:2] == ("state", "=")]
        read = [a for a in actors if matches(a, source_filters)]
        if len(read) > data_source_cap and raise_on_missing_output:
            raise RayStateApiException("Failed to retrieve all actors from the cluster ... due to data truncation.")
        result = sorted((a for a in read[:data_source_cap] if matches(a, filters)), key=lambda a: a.actor_id)
        return result[:limit]

    list_actors_mock = Mock(side_effect=list_actors)
    module = ModuleType("ray.util.state")
    module.list_actors = list_actors_mock
    monkeypatch.setitem(sys.modules, "ray.util.state", module)
    return list_ray_actors(), list_actors_mock


def test_list_ray_actors_filters_out_non_openrag_namespace(monkeypatch) -> None:
    """Ray-internal job actors live outside the ``openrag`` namespace and must be
    hidden from the admin system view (they're dead once their job ends and
    can't be restarted)."""
    actors = [
        _fake_actor("Indexer"),
        _fake_actor("WhisperPool"),
        _fake_actor("_ray_internal_job_actor_raysubmit_abc", "_ray_internal_job"),
        _fake_actor("SomethingElse", "default"),
    ]

    listing, list_actors_mock = _list_ray_actors_over(monkeypatch, actors, honor_filters=False)
    result = listing["actors"]

    # Namespace is filtered server-side so the limit applies within the openrag
    # namespace rather than across all namespaces.
    assert all(("ray_namespace", "=", "openrag") in call.kwargs["filters"] for call in list_actors_mock.call_args_list)
    # Defense-in-depth: even if the mock ignores the filter, non-openrag actors
    # are dropped by the in-comprehension guard.
    assert {a["name"] for a in result} == {"Indexer", "WhisperPool"}
    assert all(a["namespace"] == "openrag" for a in result)


def test_list_ray_actors_hides_the_dead_actor_a_restart_replaced(monkeypatch) -> None:
    """``restart_ray_actor`` kills the actor and creates a new one under the same
    name; Ray keeps the killed one's record as DEAD. Listing it showed every
    restarted actor twice (and counted it as not alive in the admin overview)."""
    actors = [
        _fake_actor("MarkerPool", state="DEAD", actor_id="old-marker"),
        _fake_actor("MarkerPool", actor_id="new-marker"),
        _fake_actor("vlmSemaphore", state="DEAD", actor_id="old-vlm-1"),
        _fake_actor("vlmSemaphore", state="DEAD", actor_id="old-vlm-2"),
        _fake_actor("vlmSemaphore", actor_id="vlm"),
    ]

    listing, _ = _list_ray_actors_over(monkeypatch, actors)

    assert sorted((a["name"], a["actor_id"]) for a in listing["actors"]) == [
        ("MarkerPool", "new-marker"),
        ("vlmSemaphore", "vlm"),
    ]


@pytest.mark.parametrize("replacement_state", ["PENDING_CREATION", "DEPENDENCIES_UNREADY", "RESTARTING"])
def test_list_ray_actors_keeps_the_dead_record_until_the_replacement_is_alive(monkeypatch, replacement_state) -> None:
    """The replaced actor's record is only dropped once its replacement has
    started successfully; a replacement still coming up (or stuck, e.g. waiting
    for a GPU) must not make the restart look done."""
    actors = [
        _fake_actor("MarkerPool", state="DEAD", actor_id="old-marker"),
        _fake_actor("MarkerPool", state=replacement_state, actor_id="new-marker"),
    ]

    listing, _ = _list_ray_actors_over(monkeypatch, actors)

    assert sorted((a["actor_id"], a["state"]) for a in listing["actors"]) == [
        ("new-marker", replacement_state),
        ("old-marker", "DEAD"),
    ]


def test_list_ray_actors_still_shows_an_actor_that_is_down(monkeypatch) -> None:
    """A DEAD actor with no live replacement is down for real: it stays listed
    (once, however many dead records share its name) so an admin can restart it."""
    actors = [
        _fake_actor("DoclingPool", state="DEAD", actor_id="old-1"),
        _fake_actor("DoclingPool", state="DEAD", actor_id="old-2"),
        _fake_actor("TaskStateManager"),
    ]

    listing, _ = _list_ray_actors_over(monkeypatch, actors)

    assert sorted((a["name"], a["state"]) for a in listing["actors"]) == [
        ("DoclingPool", "DEAD"),
        ("TaskStateManager", "ALIVE"),
    ]


def test_list_ray_actors_queries_each_state_on_its_own(monkeypatch) -> None:
    """Ray's data source applies only ``=`` filters before its cap. A single
    ``state != DEAD`` query was capped against every record, dead ones included,
    so dead records could crowd live actors out; one query per state only
    counts records in that state."""
    _, list_actors_mock = _list_ray_actors_over(monkeypatch, [])

    assert [(call.kwargs["filters"], call.kwargs["limit"]) for call in list_actors_mock.call_args_list] == [
        ([("ray_namespace", "=", "openrag"), ("state", "=", state)], 10_000)
        for state in ("DEPENDENCIES_UNREADY", "PENDING_CREATION", "ALIVE", "RESTARTING", "DEAD")
    ]


def test_list_ray_actors_still_shows_a_down_actor_past_rays_default_limit(monkeypatch) -> None:
    """Ray's default ``limit=100`` keeps the first 100 records by actor id and
    drops the rest without an error. Once restarts had piled up more than 100
    dead records, an actor that was down could fall outside that window and
    vanish from the list."""
    actors = [
        *(_fake_actor("MarkerPool", state="DEAD", actor_id=f"old-marker-{i:03}") for i in range(150)),
        _fake_actor("MarkerPool", actor_id="new-marker"),
        # Sorts after every old-marker-* record.
        _fake_actor("vlmSemaphore", state="DEAD", actor_id="old-vlm"),
    ]

    listing, _ = _list_ray_actors_over(monkeypatch, actors)

    assert sorted((a["name"], a["state"]) for a in listing["actors"]) == [
        ("MarkerPool", "ALIVE"),
        ("vlmSemaphore", "DEAD"),
    ]
    assert listing["complete"] is True


def test_list_ray_actors_past_rays_data_source_cap(monkeypatch) -> None:
    """Ray keeps up to 100k dead records across namespaces, some of them still
    among its registered actors, and reads records in no particular order. Past
    its cap, dead records read first must not push live actors out, and the
    listing must say it is incomplete instead of failing the endpoint or
    returning a partial list as if it were whole."""
    actors = [
        _fake_actor("DoclingPool", state="DEAD", actor_id="old-docling"),
        *(
            _fake_actor(
                f"_ray_internal_job_actor_raysubmit_{i}", "_ray_internal_job", state="DEAD", actor_id=f"job-{i}"
            )
            for i in range(5)
        ),
        _fake_actor("TaskStateManager"),
        _fake_actor("MarkerPool", actor_id="new-marker"),
    ]
    logger = Mock()
    monkeypatch.setattr(di.workers, "logger", logger)

    listing, _ = _list_ray_actors_over(monkeypatch, actors, data_source_cap=3)

    assert sorted((a["name"], a["state"]) for a in listing["actors"]) == [
        ("DoclingPool", "DEAD"),
        ("MarkerPool", "ALIVE"),
        ("TaskStateManager", "ALIVE"),
    ]
    assert listing["complete"] is False
    logger.warning.assert_called_once()


def test_list_ray_actors_counts_a_full_page_as_incomplete(monkeypatch) -> None:
    """The API server's ``limit`` cuts without an error. It can only bite when
    the data source's cap is set above it, and then the listing can't tell a
    cut from an exact fit, so a full page is reported as incomplete."""
    monkeypatch.setattr(di.workers, "_ACTOR_LIST_LIMIT", 2)
    actors = [_fake_actor(name) for name in ("Indexer", "MarkerPool", "TaskStateManager")]

    listing, _ = _list_ray_actors_over(monkeypatch, actors)

    assert len(listing["actors"]) == 2
    assert listing["complete"] is False


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


@pytest.mark.asyncio
async def test_task_state_manager_restart_preserves_cached_handles(monkeypatch) -> None:
    ready_ref = asyncio.get_running_loop().create_future()
    ready_ref.set_result(True)
    ready_method = SimpleNamespace(remote=Mock(return_value=ready_ref))
    actor = SimpleNamespace(
        _actor_id=SimpleNamespace(hex=Mock(return_value="same-actor-id")),
        get_pool_info=ready_method,
        supports_in_place_restart=SimpleNamespace(),
        renew_file_delete=SimpleNamespace(),
    )
    factory = Mock()
    monkeypatch.setattr("di.workers.get_actor_creation_map", lambda: {"TaskStateManager": factory})
    monkeypatch.setattr(ray, "get_actor", Mock(return_value=actor))
    kill = Mock()
    monkeypatch.setattr(ray, "kill", kill)

    assert await restart_ray_actor("TaskStateManager") == "same-actor-id"

    kill.assert_called_once_with(actor, no_restart=False)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_other_actor_restart_still_recreates_the_actor(monkeypatch) -> None:
    old_actor = SimpleNamespace()
    new_actor = SimpleNamespace(_actor_id=SimpleNamespace(hex=Mock(return_value="new-actor-id")))
    factory = Mock(return_value=new_actor)
    monkeypatch.setattr("di.workers.get_actor_creation_map", lambda: {"MarkerPool": factory})
    monkeypatch.setattr(ray, "get_actor", Mock(return_value=old_actor))
    kill = Mock()
    monkeypatch.setattr(ray, "kill", kill)

    assert await restart_ray_actor("MarkerPool") == "new-actor-id"

    kill.assert_called_once_with(old_actor, no_restart=True)
    factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_legacy_task_state_manager_without_restart_policy_is_recreated(monkeypatch) -> None:
    old_actor = SimpleNamespace()
    new_actor = SimpleNamespace(_actor_id=SimpleNamespace(hex=Mock(return_value="new-task-state-id")))
    factory = Mock(return_value=new_actor)
    monkeypatch.setattr("di.workers.get_actor_creation_map", lambda: {"TaskStateManager": factory})
    monkeypatch.setattr(ray, "get_actor", Mock(return_value=old_actor))
    kill = Mock()
    monkeypatch.setattr(ray, "kill", kill)

    assert await restart_ray_actor("TaskStateManager") == "new-task-state-id"

    kill.assert_called_once_with(old_actor, no_restart=True)
    factory.assert_called_once_with()
