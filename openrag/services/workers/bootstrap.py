"""Ray-actor bootstrap for the worker pool.

Imported at startup (explicitly by ``main.py``) to create the long-lived
detached actors the request path looks up by name:

* TaskStateManager  — shared task-state actor
* llmSemaphore / vlmSemaphore / audioSemaphore — cluster-wide rate limiters

The GPU parser pools (MarkerPool / DoclingPool / WhisperPool / WhisperActor)
are *not* created here: their loaders self-provision them on first use via
``get_or_create_actor``. Bootstrap only registers their restart factories
(see ``register_parser_pool_restart_factories``) so the admin restart
endpoint can manage them from the API process.

This is the **only** non-startup module outside ``services/workers/`` that
imports Ray — the phase-9 plan permits Ray in ``services/workers/`` plus
the ``main.py`` startup sequence. Before phase 9 the same logic lived in
``utils/dependencies.py``; that location violated the "Ray only in
workers + startup" rule, so the bootstrap moved here.

``actor_creation_map`` is read by :mod:`api.routers.admin.cluster` to
restart a named actor on-demand from the admin endpoint.
"""

from functools import wraps
from typing import TYPE_CHECKING

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.config.root import Settings


logger = get_logger()

actor_creation_map: dict[str, callable] = {}
_settings: "Settings | None" = None


def _require_settings() -> "Settings":
    if _settings is None:
        raise RuntimeError("Worker bootstrap has not been initialized.")
    return _settings


def _track_actor(func):
    @wraps(func)
    def wrapper(name, cls, namespace="openrag", remote_args=(), **options):
        actor_creation_map[name] = lambda: func(name, cls, namespace=namespace, remote_args=remote_args, **options)
        return func(name, cls, namespace=namespace, remote_args=remote_args, **options)

    return wrapper


@_track_actor
def get_or_create_actor(name, cls, namespace="openrag", remote_args=(), **options):
    # ``get_if_exists`` makes get-or-create atomic inside Ray: concurrent callers
    # (e.g. the multiple Ray Serve replicas that all run worker bootstrap in their
    # lifespan at once, or a per-preset backend selecting the same pool) either all
    # attach to the same actor or exactly one creates it and the rest attach. The
    # previous get_actor()-then-create pattern had a TOCTOU race — two callers both
    # saw "not found" and collided on creation, crashing with "Failed to look up actor".
    return cls.options(name=name, namespace=namespace, get_if_exists=True, **options).remote(*remote_args)


def get_task_state_manager():
    from services.workers.task_state import TASK_STATE_MANAGER_ACTOR_NAME, TaskStateManager

    return get_or_create_actor(TASK_STATE_MANAGER_ACTOR_NAME, TaskStateManager, lifetime="detached")


def register_parser_pool_restart_factories() -> None:
    """Register restart factories for the lazily-created parser pools.

    Parser pools are provisioned on first use, usually inside an indexer
    worker process — but ``actor_creation_map`` is process-local, so the API
    process would never learn how to (re)create them and
    ``POST /cluster/{actor}/restart`` would 404 for every parser pool.
    Register the factories for *all* pools here without creating any actor
    (a per-preset ``parsing_strategy`` can bring up any of them at runtime,
    not just the globally-configured backend). Restarting a pool that was
    never created simply creates it.

    Imports stay inside the factories so the API process doesn't pay for
    the heavy parser dependencies (torch, marker, docling) at startup.
    """

    def marker_pool():
        from services.workers.parsers.marker_workers import MarkerPool

        return get_or_create_actor("MarkerPool", MarkerPool, lifetime="detached")

    def docling_pool():
        from services.workers.parsers.docling_workers import DoclingPool

        return get_or_create_actor("DoclingPool", DoclingPool, lifetime="detached")

    def whisper_pool():
        from services.workers.parsers.whisper_workers import WhisperPool

        return get_or_create_actor("WhisperPool", WhisperPool, lifetime="detached")

    def whisper_actor():
        from services.workers.parsers.whisper_workers import WhisperActor, whisper_actor_options

        config = _require_settings()
        return get_or_create_actor("WhisperActor", WhisperActor, lifetime="detached", **whisper_actor_options(config))

    actor_creation_map["MarkerPool"] = marker_pool
    actor_creation_map["DoclingPool"] = docling_pool
    actor_creation_map["WhisperPool"] = whisper_pool
    actor_creation_map["WhisperActor"] = whisper_actor


def init_llm_semaphore():
    from services.inference.distributed_semaphore import DistributedSemaphoreActor

    config = _require_settings()
    return get_or_create_actor(
        "llmSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.semaphore.llm_semaphore,),
    )


def init_vlm_semaphore():
    from services.inference.distributed_semaphore import DistributedSemaphoreActor

    config = _require_settings()
    return get_or_create_actor(
        "vlmSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.semaphore.vlm_semaphore,),
    )


def init_audio_semaphore():
    from services.inference.distributed_semaphore import DistributedSemaphoreActor

    config = _require_settings()
    return get_or_create_actor(
        "audioSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.loader.transcriber.max_concurrent_chunks,),
    )


def initialize_worker_bootstrap(settings: "Settings") -> None:
    """Create the detached worker actors required by the request path.

    Parser pools are exempt: they are created lazily by their loaders, and
    bootstrap only registers their restart factories.
    """
    global _settings
    _settings = settings

    init_llm_semaphore()
    init_vlm_semaphore()
    init_audio_semaphore()
    get_task_state_manager()
    register_parser_pool_restart_factories()
