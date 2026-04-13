from functools import wraps

import ray
from services.workers.indexer import Indexer, TaskStateManager
from services.workers.loaders.audio import WhisperActor, WhisperPool
from services.workers.loaders.pdf_loaders.docling2 import DoclingPool
from services.workers.loaders.pdf_loaders.marker import MarkerPool
from services.workers.loaders.serializer import DocSerializer
from services.storage.vectordb.vectordb import ConnectorFactory
from services.utils import DistributedSemaphoreActor
from config import load_config
from utils.logger import get_logger

# load config
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


def get_indexer():
    return get_or_create_actor("Indexer", Indexer, lifetime="detached")


def get_vectordb():
    vectordb_cls = ConnectorFactory().get_vectordb_cls()
    return get_or_create_actor("Vectordb", vectordb_cls, lifetime="detached")


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
vectordb = get_vectordb()
indexer = get_indexer()
