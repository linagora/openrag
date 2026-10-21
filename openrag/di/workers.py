"""Worker bootstrap accessors for API startup and admin controls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()

# All OpenRag-managed actors are created in this Ray namespace (see
# services/workers/bootstrap.py).
OPENRAG_NAMESPACE = "openrag"

# Records per ``list_actors`` query. Ray's API server rejects a higher limit
# (``RAY_MAX_LIMIT_FROM_API_SERVER``), and it is also where Ray's data source
# stops reading. Ray's own default, 100, drops the rest without a word.
_ACTOR_LIST_LIMIT = 10_000

# Every actor state but DEAD, queried one by one (see ``list_ray_actors``).
_LIVE_STATES = ("DEPENDENCIES_UNREADY", "PENDING_CREATION", "ALIVE", "RESTARTING")


def _list_openrag_actors_in(state: str) -> tuple[list[Any], bool]:
    """Return the ``openrag`` actors in ``state``, and whether Ray returned them all.

    Ray's data source applies a ``state =`` filter itself, so only actors in
    ``state`` count towards its cap; ``ray_namespace`` is applied after the cap,
    so the cap covers that state across every namespace. When Ray reports it
    stopped short, the actors it did return are listed rather than failing the
    whole listing. A full page counts as incomplete too, in case the data
    source's cap was raised above ``_ACTOR_LIST_LIMIT``.
    """
    from ray.util.state import list_actors
    from ray.util.state.exception import RayStateApiException

    filters = [("ray_namespace", "=", OPENRAG_NAMESPACE), ("state", "=", state)]
    try:
        actors = list_actors(filters=filters, limit=_ACTOR_LIST_LIMIT)
    except RayStateApiException as exc:
        # Usually the data source stopping short. Anything else (the API server
        # failing) fails the retry too, and raises as before.
        logger.warning(
            "Ray could not return every {} actor record, retrying without the completeness check: {}", state, exc
        )
        return list_actors(filters=filters, limit=_ACTOR_LIST_LIMIT, raise_on_missing_output=False), False
    return actors, len(actors) < _ACTOR_LIST_LIMIT


def list_ray_actors() -> dict[str, Any]:
    """List OpenRag-managed Ray actors without exposing Ray imports to API routers.

    Returns ``{"actors": [...], "complete": bool}``. ``complete`` is false when
    Ray could not return every record a query matched; the list may then be
    missing actors.

    Restricted to the ``openrag`` namespace so Ray-internal actors (e.g.
    ``_ray_internal_job_actor_raysubmit_*`` job supervisors, which appear dead
    once their job finishes and have no creation function to restart) don't show
    up in the admin system view.

    The namespace is filtered server-side via ``list_actors(filters=...)`` so
    that the ``limit`` applies *within* the ``openrag`` namespace rather than
    across all namespaces — otherwise accumulated dead Ray-internal job actors
    could push OpenRag's own actors out of the result window. The
    in-comprehension check is kept as cheap defense-in-depth.

    ``restart_ray_actor`` replaces most actors with a new one, and Ray keeps the
    killed actor's record as ``DEAD``, so every restart would otherwise leave a
    dead row next to its live replacement (and count against "alive" in the
    admin overview). A ``DEAD`` record is hidden only once an actor holding its
    name is ``ALIVE`` — the replacement started successfully. While it is still
    pending or restarting, or if it failed, the dead record stays listed (once
    per name) so the admin can see the restart hasn't taken.

    Each state is its own query, with an ``=`` filter. Ray's data source reads
    at most ``_ACTOR_LIST_LIMIT`` records and applies only ``=`` filters before
    that cap, in no particular order, and it keeps up to 100k dead records
    across all namespaces. One query for every state but DEAD could therefore
    lose live actors behind dead records; one query per state only counts
    records in that state.
    """
    actors: list[Any] = []
    complete = True
    for state in _LIVE_STATES:
        in_state, returned_all = _list_openrag_actors_in(state)
        actors.extend(in_state)
        complete = complete and returned_all
    actors.sort(key=lambda actor: actor.actor_id)
    dead, returned_all = _list_openrag_actors_in("DEAD")
    complete = complete and returned_all

    # Names whose dead records add nothing: an ALIVE actor holds the name, or a
    # dead record for it is already listed.
    covered_names = {actor.name for actor in actors if actor.name and actor.state == "ALIVE"}
    for actor in dead:
        if actor.name and actor.name in covered_names:
            continue
        if actor.name:
            covered_names.add(actor.name)
        actors.append(actor)

    return {
        "actors": [
            {
                "actor_id": actor.actor_id,
                "name": actor.name,
                "class_name": actor.class_name,
                "state": actor.state,
                "namespace": actor.ray_namespace,
            }
            for actor in actors
            if actor.ray_namespace == OPENRAG_NAMESPACE
        ],
        "complete": complete,
    }


def ensure_worker_bootstrap(settings: Any) -> None:
    """Initialize the worker bootstrap after Ray has been initialized."""
    from services.workers.bootstrap import initialize_worker_bootstrap

    initialize_worker_bootstrap(settings)


def get_actor_creation_map() -> Mapping[str, Callable[[], Any]]:
    """Return the Ray actor restart factories created by worker bootstrap."""
    from services.workers.bootstrap import actor_creation_map

    return actor_creation_map


async def restart_ray_actor(actor_name: str) -> str:
    """Restart a named Ray actor and return its actor id."""
    import asyncio

    import ray
    from core.utils.exceptions import ServiceUnavailableError
    from services.workers.ray_utils import call_ray_actor_method_with_timeout

    actor_creation_map = get_actor_creation_map()
    if actor_name not in actor_creation_map:
        raise KeyError(actor_name)

    try:
        actor = ray.get_actor(actor_name, namespace=OPENRAG_NAMESPACE)
    except ValueError:
        actor = None

    restart_capability = getattr(actor, "supports_in_place_restart", None) if actor is not None else None
    renewable_fences = getattr(actor, "renew_file_delete", None) if actor is not None else None
    if actor_name == "TaskStateManager" and restart_capability is not None and renewable_fences is not None:
        # This actor is shared through handles cached by API services and
        # indexer workers. Let Ray reconstruct the same actor incarnation;
        # replacing it with a new actor would leave every cached handle bound
        # to the dead one. Poll a normal actor method because Ray's built-in
        # readiness future can complete just before ordinary calls stop seeing
        # ActorUnavailableError.
        actor_id = actor._actor_id.hex()
        ray.kill(actor, no_restart=False)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while loop.time() < deadline:
            try:
                remaining = max(0.01, min(1.0, deadline - loop.time()))
                await call_ray_actor_method_with_timeout(
                    submit=lambda: actor.get_pool_info.remote(),
                    timeout=remaining,
                    task_description="TaskStateManager restart readiness",
                )
                return actor_id
            except (ServiceUnavailableError, TimeoutError):
                await asyncio.sleep(0.1)
        raise ServiceUnavailableError(
            "Task state manager did not recover after restart",
            code="TASK_STATE_RECOVERY_TIMEOUT",
        )

    if actor is not None:
        ray.kill(actor, no_restart=True)

    new_actor = actor_creation_map[actor_name]()
    return new_actor._actor_id.hex()
