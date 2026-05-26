"""Ray-actor bootstrap for the worker pool.

Imported at startup (explicitly by ``main.py``) to create the long-lived
detached actors the request path looks up by name:

* TaskStateManager  — shared task-state actor
* DocSerializer     — loader dispatcher
* MarkerPool / DoclingPool / WhisperPool / WhisperActor — GPU parsers
* llmSemaphore / vlmSemaphore / audioSemaphore — cluster-wide rate limiters

This is the **only** non-startup module outside ``services/workers/`` that
imports Ray — the phase-9 plan permits Ray in ``services/workers/`` plus
the ``main.py`` startup sequence. Before phase 9 the same logic lived in
``utils/dependencies.py``; that location violated the "Ray only in
workers + startup" rule, so the bootstrap moved here.

``actor_creation_map`` is read by ``routers/actors.py`` to restart a
named actor on-demand from the admin endpoint.
"""

from functools import wraps

import ray
from components.indexer.loaders.audio import WhisperActor, WhisperPool
from components.indexer.loaders.pdf_loaders.docling2 import DoclingPool
from components.indexer.loaders.pdf_loaders.marker import MarkerPool
from components.indexer.loaders.serializer import DocSerializer
from config import load_config
from services.inference.distributed_semaphore import DistributedSemaphoreActor
from services.workers.task_state import TaskStateManager
from utils.logger import get_logger

config = load_config()
logger = get_logger()

actor_creation_map: dict[str, callable] = {}


def _track_actor(func):
    @wraps(func)
    def wrapper(name, cls, namespace="openrag", remote_args=(), **options):
        actor_creation_map[name] = lambda: func(name, cls, namespace=namespace, remote_args=remote_args, **options)
        return func(name, cls, namespace=namespace, remote_args=remote_args, **options)

    return wrapper


@_track_actor
def get_or_create_actor(name, cls, namespace="openrag", remote_args=(), **options):
    try:
        return ray.get_actor(name, namespace=namespace)
    except ValueError:
        return cls.options(name=name, namespace=namespace, **options).remote(*remote_args)
    except Exception:
        raise


def get_task_state_manager():
    return get_or_create_actor("TaskStateManager", TaskStateManager, lifetime="detached")


def get_serializer():
    return get_or_create_actor("DocSerializer", DocSerializer, lifetime="detached")


def get_marker_pool():
    pdf_loader = config.loader.file_loaders.pdf
    match pdf_loader:
        case "DoclingLoader2":
            return get_or_create_actor("DoclingPool", DoclingPool, lifetime="detached")
        case "MarkerLoader":
            return get_or_create_actor("MarkerPool", MarkerPool, lifetime="detached")


def init_audio_actor():
    use_whisper_lang_detector = config.loader.transcriber.use_whisper_lang_detector
    file_loaders = config.loader.file_loaders
    loader_values = set(file_loaders.values()) if file_loaders else set()

    if "LocalWhisperLoader" in loader_values:
        return get_or_create_actor("WhisperPool", WhisperPool, lifetime="detached")

    if "OpenAIAudioLoader" in loader_values and use_whisper_lang_detector:
        return get_or_create_actor("WhisperActor", WhisperActor, lifetime="detached")


def init_llm_semaphore():
    return get_or_create_actor(
        "llmSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.semaphore.llm_semaphore,),
    )


def init_vlm_semaphore():
    return get_or_create_actor(
        "vlmSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.semaphore.vlm_semaphore,),
    )


def init_audio_semaphore():
    return get_or_create_actor(
        "audioSemaphore",
        DistributedSemaphoreActor,
        lifetime="detached",
        remote_args=(config.loader.transcriber.max_concurrent_chunks,),
    )


init_llm_semaphore()
init_vlm_semaphore()
init_audio_semaphore()
init_audio_actor()
get_marker_pool()

task_state_manager = get_task_state_manager()
serializer = get_serializer()
