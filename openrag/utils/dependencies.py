import ray
import ray.actor
from components.indexer.indexer import Indexer, TaskStateManager
from components.indexer.loaders.pdf_loaders.docling2 import DoclingPool
from components.indexer.loaders.pdf_loaders.marker import MarkerPool
from components.indexer.loaders.serializer import DocSerializer
from components.indexer.vectordb.vectordb import ConnectorFactory
from config import load_config


def get_or_create_actor(name, cls, namespace="openrag", **options):
    try:
        return ray.get_actor(name, namespace=namespace)
    except ValueError:
        return cls.options(name=name, namespace=namespace, **options).remote()
    except Exception:
        raise


# load config
config = load_config()


def get_task_state_manager():
    return get_or_create_actor(
        "TaskStateManager", TaskStateManager, lifetime="detached"
    )


def get_serializer():
    return get_or_create_actor("DocSerializer", DocSerializer, lifetime="detached")


def get_marker_pool():
    pdf_loader = config.loader.file_loaders.get("pdf")
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


task_state_manager = get_task_state_manager()
serializer = get_serializer()
vectordb = get_vectordb()
indexer = get_indexer()
marker_pool = get_marker_pool()
