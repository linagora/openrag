"""Worker bootstrap accessors for API startup and admin controls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# All OpenRag-managed actors are created in this Ray namespace (see
# services/workers/bootstrap.py).
OPENRAG_NAMESPACE = "openrag"


def list_ray_actors() -> list[dict[str, str | None]]:
    """List OpenRag-managed Ray actors without exposing Ray imports to API routers.

    Restricted to the ``openrag`` namespace so Ray-internal actors (e.g.
    ``_ray_internal_job_actor_raysubmit_*`` job supervisors, which appear dead
    once their job finishes and have no creation function to restart) don't show
    up in the admin system view.

    The namespace is filtered server-side via ``list_actors(filters=...)`` so
    that Ray's default ``limit=100`` applies *within* the ``openrag`` namespace
    rather than across all namespaces — otherwise accumulated dead Ray-internal
    job actors could push OpenRag's own actors out of the result window. The
    in-comprehension check is kept as cheap defense-in-depth.
    """
    from ray.util.state import list_actors

    return [
        {
            "actor_id": actor.actor_id,
            "name": actor.name,
            "class_name": actor.class_name,
            "state": actor.state,
            "namespace": actor.ray_namespace,
        }
        for actor in list_actors(filters=[("ray_namespace", "=", OPENRAG_NAMESPACE)])
        if actor.ray_namespace == OPENRAG_NAMESPACE
    ]


def ensure_worker_bootstrap(settings: Any) -> None:
    """Initialize the worker bootstrap after Ray has been initialized."""
    from services.workers.bootstrap import initialize_worker_bootstrap

    initialize_worker_bootstrap(settings)


def get_actor_creation_map() -> Mapping[str, Callable[[], Any]]:
    """Return the Ray actor restart factories created by worker bootstrap."""
    from services.workers.bootstrap import actor_creation_map

    return actor_creation_map


def restart_ray_actor(actor_name: str) -> str:
    """Restart a named Ray actor and return the new actor id."""
    import ray

    actor_creation_map = get_actor_creation_map()
    if actor_name not in actor_creation_map:
        raise KeyError(actor_name)

    try:
        actor = ray.get_actor(actor_name, namespace=OPENRAG_NAMESPACE)
        ray.kill(actor, no_restart=True)
    except ValueError:
        pass

    new_actor = actor_creation_map[actor_name]()
    return new_actor._actor_id.hex()
