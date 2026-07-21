"""Drain and remove a superseded detached indexer actor generation.

Run this only after traffic has stopped reaching API replicas that use the
generation being retired. Protocol-aware generations are drained before they
are removed. The original unversioned generation cannot report its state, so
retiring it requires an explicit operator confirmation. The same confirmation
is required when a dispatcher is unavailable but its workers are still alive.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from typing import Any

import ray

from .ray_utils import call_ray_actor_with_timeout

_LEGACY_GENERATION = "legacy"
_LEGACY_DISPATCHER_NAME = "IndexerPoolDispatcher"
_LEGACY_WORKER_PATTERN = re.compile(r"^IndexerWorker-\d+$")


def _actor_name(actor: Any) -> str:
    if isinstance(actor, dict):
        return str(actor.get("name") or "")
    return str(getattr(actor, "name", ""))


def _actor_state(actor: Any) -> str:
    if isinstance(actor, dict):
        return str(actor.get("state") or "")
    return str(getattr(actor, "state", ""))


def _generation_actor_names(actors: list[Any], generation: str) -> tuple[str, list[str]]:
    if generation == _LEGACY_GENERATION:
        dispatcher_name = _LEGACY_DISPATCHER_NAME
        worker_names = sorted(
            name
            for actor in actors
            if _actor_state(actor) == "ALIVE"
            and (name := _actor_name(actor))
            and _LEGACY_WORKER_PATTERN.fullmatch(name)
        )
        return dispatcher_name, worker_names

    dispatcher_name = f"IndexerPoolDispatcher-{generation}"
    worker_prefix = f"IndexerWorker-{generation}-"
    worker_names = sorted(
        name
        for actor in actors
        if _actor_state(actor) == "ALIVE" and (name := _actor_name(actor)) and name.startswith(worker_prefix)
    )
    return dispatcher_name, worker_names


def _get_actor(name: str, namespace: str) -> Any | None:
    try:
        return ray.get_actor(name, namespace=namespace)
    except ValueError:
        return None


def _kill_actor(name: str, namespace: str) -> bool:
    actor = _get_actor(name, namespace)
    if actor is None:
        return False
    ray.kill(actor, no_restart=True)
    return True


def _drain_timeout(generation: str, timeout: float) -> TimeoutError:
    return TimeoutError(
        f"Indexer generation {generation!r} did not drain within {timeout:g} seconds; actors were kept."
    )


async def _drain_protocol_generation(
    dispatcher: Any,
    generation: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    """Drain one protocol-aware generation within one end-to-end deadline."""
    deadline = time.monotonic() + timeout

    async def call_with_remaining_timeout(method: Any, description: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _drain_timeout(generation, timeout)
        future = method.remote()
        try:
            return await call_ray_actor_with_timeout(future, remaining, description)
        except TimeoutError as exc:
            raise _drain_timeout(generation, timeout) from exc

    status = await call_with_remaining_timeout(
        dispatcher.begin_drain,
        f"Begin draining indexer generation {generation}",
    )
    while int(status.get("inflight_jobs", 0)) > 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _drain_timeout(generation, timeout)
        await asyncio.sleep(min(max(poll_interval, 0.0), remaining))
        status = await call_with_remaining_timeout(
            dispatcher.status,
            f"Check indexer generation {generation} drain status",
        )
    return status


def retire_generation(
    generation: str,
    *,
    namespace: str,
    timeout: float,
    poll_interval: float,
    confirm_legacy_idle: bool,
    confirm_workers_idle: bool = False,
) -> list[str]:
    """Drain and remove one actor generation, returning removed actor names."""
    from ray.util.state import list_actors

    actors = list(
        list_actors(
            filters=[("ray_namespace", "=", namespace)],
            limit=10_000,
        )
    )
    dispatcher_name, worker_names = _generation_actor_names(actors, generation)
    dispatcher = _get_actor(dispatcher_name, namespace)

    if generation == _LEGACY_GENERATION:
        if (dispatcher is not None or worker_names) and not confirm_legacy_idle:
            raise RuntimeError(
                "The legacy indexer cannot report active work. Stop old API traffic, verify its jobs are idle, "
                "then rerun with --confirm-legacy-idle."
            )

    removed: list[str] = []
    if dispatcher is None:
        if worker_names and not (confirm_legacy_idle or confirm_workers_idle):
            raise RuntimeError(
                "The indexer dispatcher is unavailable, so active worker jobs cannot be checked. "
                "Verify the workers are idle, then rerun with --confirm-workers-idle."
            )
        for worker_name in worker_names:
            if _kill_actor(worker_name, namespace):
                removed.append(worker_name)
        return removed

    if generation != _LEGACY_GENERATION:
        status = asyncio.run(
            _drain_protocol_generation(
                dispatcher,
                generation,
                timeout=timeout,
                poll_interval=poll_interval,
            )
        )
        worker_names = list(status.get("worker_names") or worker_names)

    if _kill_actor(dispatcher_name, namespace):
        removed.append(dispatcher_name)
    for worker_name in worker_names:
        if _kill_actor(worker_name, namespace):
            removed.append(worker_name)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", required=True, help="Protocol generation to retire, for example v2 or legacy")
    parser.add_argument("--namespace", default="openrag")
    parser.add_argument("--ray-address", default="auto")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--confirm-legacy-idle",
        action="store_true",
        help="Confirm old API traffic is stopped and the unversioned generation has no active work",
    )
    parser.add_argument(
        "--confirm-workers-idle",
        action="store_true",
        help="Confirm workers are idle when their dispatcher is unavailable",
    )
    args = parser.parse_args()

    ray.init(address=args.ray_address, ignore_reinit_error=True)
    removed = retire_generation(
        args.generation,
        namespace=args.namespace,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        confirm_legacy_idle=args.confirm_legacy_idle,
        confirm_workers_idle=args.confirm_workers_idle,
    )
    if removed:
        print("Removed indexer actors: " + ", ".join(removed))
    else:
        print(f"No live indexer actors found for generation {args.generation!r}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
