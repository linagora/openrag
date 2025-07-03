import asyncio
import atexit
import threading
from abc import ABCMeta
from pathlib import Path

import ray
from config.config import load_config
from langchain_core.documents.base import Document


class SingletonMeta(type):
    _instances = {}
    _lock = threading.Lock()  # Ensures thread safety

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:  # First check (not thread-safe yet)
            with cls._lock:  # Prevents multiple threads from creating instances
                if cls not in cls._instances:  # Second check (double-checked locking)
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]


class SingletonABCMeta(ABCMeta, SingletonMeta):
    pass


class LLMSemaphore(metaclass=SingletonMeta):
    def __init__(self, max_concurrent_ops: int):
        if max_concurrent_ops <= 0:
            raise ValueError("max_concurrent_ops must be a positive integer")
        self.max_concurrent_ops = max_concurrent_ops
        self._semaphore = asyncio.Semaphore(max_concurrent_ops)
        atexit.register(self.cleanup)

    async def __aenter__(self):
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._semaphore.release()

    async def acquire(self):
        await self._semaphore.acquire()

    def release(self):
        self._semaphore.release()

    def cleanup(self):
        """Ensure semaphore is released at shutdown"""
        while self._semaphore.locked():
            self._semaphore.release()


@ray.remote(max_restarts=-1, max_concurrency=100000)
class DistributedSemaphoreActor:
    def __init__(self, max_concurrent_ops: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_ops)

    async def acquire(self):
        await self.semaphore.acquire()

    async def release(self):
        self.semaphore.release()

    def cleanup(self):
        while self.semaphore.locked():
            self.semaphore.release()


class DistributedSemaphore:
    # https://chat.deepseek.com/a/chat/s/890dbcc0-2d3f-4819-af9d-774b892905bc
    def __init__(
        self,
        name: str = "llmSemaphore",
        namespace="ragondin",
        max_concurrent_ops: int = 10,
    ):
        try:
            actor = ray.get_actor(
                name, namespace=namespace
            )  # reuse existing actor if it exists
        except ValueError:
            # create new actor if it doesn't exist
            actor = DistributedSemaphoreActor.options(
                name=name, namespace=namespace
            ).remote(max_concurrent_ops)

        self._actor = actor

    async def __aenter__(self):
        await self._actor.acquire.remote()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._actor.release.remote()

    def cleanup(self):
        ray.get(self._actor.cleanup.remote())


def load_sys_template(file_path: Path) -> tuple[str, str]:
    with open(file_path, mode="r") as f:
        sys_msg = f.read()
        return sys_msg


def format_context(docs: list[Document]) -> str:
    if not docs:
        return "No document found from the database"

    context = "Extracted documents:\n"
    for i, doc in enumerate(docs, start=1):
        doc_id = f"[doc_{i}]"
        document = f"""
        *source*: {doc_id}
        content: \n{doc.page_content.strip()}\n
        """
        context += document
        context += "-" * 40 + "\n\n"

    return context


# Global variables
config = load_config()

# llmSemaphore = LLMSemaphore(max_concurrent_ops=config.semaphore.llm_semaphore)
llmSemaphore = DistributedSemaphore(
    name="llmSemaphore", max_concurrent_ops=config.semaphore.llm_semaphore
)
vlmSemaphore = DistributedSemaphore(
    name="vlmSemaphore", max_concurrent_ops=config.semaphore.vlm_semaphore
)
