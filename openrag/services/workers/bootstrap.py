"""Ray-actor bootstrap for the worker pool.

Imported at startup (explicitly by ``main.py``) to create the long-lived
detached actors the request path looks up by name:

* TaskStateManager  — shared task-state actor
* MarkerPool / DoclingPool / WhisperPool / WhisperActor — GPU parsers
* llmSemaphore / vlmSemaphore / audioSemaphore — cluster-wide rate limiters

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
    from services.workers.task_state import TaskStateManager

    return get_or_create_actor("TaskStateManager", TaskStateManager, lifetime="detached")


def get_marker_pool():
    from services.workers.parsers.docling_workers import DoclingPool
    from services.workers.parsers.marker_workers import MarkerPool

    config = _require_settings()
    pdf_loader = config.loader.file_loaders.pdf
    match pdf_loader:
        case "DoclingLoader":
            return get_or_create_actor("DoclingPool", DoclingPool, lifetime="detached")
        case "MarkerLoader":
            return get_or_create_actor("MarkerPool", MarkerPool, lifetime="detached")


def init_audio_actor():
    from services.workers.parsers.whisper_workers import WhisperActor, WhisperPool, whisper_actor_options

    config = _require_settings()
    use_whisper_lang_detector = config.loader.transcriber.use_whisper_lang_detector
    file_loaders = config.loader.file_loaders
    loader_values = set(file_loaders.values()) if file_loaders else set()

    if "LocalWhisperLoader" in loader_values:
        return get_or_create_actor("WhisperPool", WhisperPool, lifetime="detached")

    if "OpenAIAudioLoader" in loader_values and use_whisper_lang_detector:
        return get_or_create_actor("WhisperActor", WhisperActor, lifetime="detached", **whisper_actor_options(config))


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
    """Create the detached worker actors required by the request path."""
    global _settings
    _settings = settings

    init_llm_semaphore()
    init_vlm_semaphore()
    init_audio_semaphore()
    init_audio_actor()
    get_marker_pool()
    get_task_state_manager()
