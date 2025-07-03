import ray
import ray.actor
from components import ABCVectorDB
from components.indexer.indexer import Indexer, TaskStateManager
from components.indexer.loaders.pdf_loaders.marker import MarkerPool
from components.indexer.loaders.serializer import SerializerQueue
from config import load_config


def get_or_create_actor(name, cls, namespace="ragondin", **options):
    try:
        return ray.get_actor(name, namespace=namespace)
    except ValueError:
        return cls.options(name=name, namespace=namespace, **options).remote()
    except:
        raise


class VDBProxy:
    """Class that delegates method calls to the remote vectordb."""

    def __init__(self, indexer_actor: ray.actor.ActorHandle):
        self.indexer_actor = indexer_actor  # Reference to the remote actor

    def __getattr__(self, method_name):
        # Check if the method is async on the remote vectordb
        is_async = ray.get(self.indexer_actor._is_method_async.remote(method_name))

        if is_async:
            # Return an async coroutine for async methods
            async def async_wrapper(*args, **kwargs):
                result_ref = self.indexer_actor._delegate_vdb_call.remote(
                    method_name, *args, **kwargs
                )
                return await result_ref

            return async_wrapper

        else:
            # Return a blocking wrapper for sync methods
            def sync_wrapper(*args, **kwargs):
                return ray.get(
                    self.indexer_actor._delegate_vdb_call.remote(
                        method_name, *args, **kwargs
                    )
                )

            return sync_wrapper


# load config
config = load_config()


def get_task_state_manager():
    return get_or_create_actor(
        "TaskStateManager", TaskStateManager, lifetime="detached"
    )


def get_serializer_queue():
    return get_or_create_actor("SerializerQueue", SerializerQueue)


def get_marker_pool():
    if config.loader.file_loaders.get("pdf") == "MarkerLoader":
        return get_or_create_actor("MarkerPool", MarkerPool)


def get_indexer():
    return get_or_create_actor("Indexer", Indexer)


def get_vectordb() -> ABCVectorDB:
    indexer = ray.get_actor("Indexer", namespace="ragondin")
    return VDBProxy(indexer_actor=indexer)


indexer = get_indexer()
marker_pool = get_marker_pool()
vectordb = get_vectordb()
