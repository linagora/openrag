"""Worker bootstrap accessors for API startup and admin controls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def list_ray_actors() -> list[dict[str, str | None]]:
    """List Ray actors without exposing Ray imports to API routers."""
    from ray.util.state import list_actors

    return [
        {
            "actor_id": actor.actor_id,
            "name": actor.name,
            "class_name": actor.class_name,
            "state": actor.state,
            "namespace": actor.ray_namespace,
        }
        for actor in list_actors()
    ]


def ensure_worker_bootstrap() -> None:
    """Import the worker bootstrap after Ray has been initialized."""
    import services.workers.bootstrap  # noqa: F401


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
        actor = ray.get_actor(actor_name, namespace="openrag")
        ray.kill(actor, no_restart=True)
    except ValueError:
        pass

    new_actor = actor_creation_map[actor_name]()
    return new_actor._actor_id.hex()
