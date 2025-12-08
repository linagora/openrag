import asyncio
import atexit
import threading
from abc import ABCMeta

import ray
from config import load_config
from fast_langdetect import LangDetectConfig, LangDetector
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from utils.logger import get_logger

# Global variables
config = load_config()
logger = get_logger()


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


@ray.remote(max_restarts=-1, max_concurrency=config.ray.semaphore.concurrency)
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
        namespace="openrag",
        max_concurrent_ops: int = 10,
        lazy: bool = True,
    ):
        self._actor = None
        self._name = name
        self._namespace = namespace
        self._max_concurrent_ops = max_concurrent_ops
        self._lazy = lazy

        if not lazy:
            self._init_actor()

    def _init_actor(self):
        if self._actor is None:
            try:
                self._actor = ray.get_actor(
                    self._name, namespace=self._namespace
                )  # reuse existing actor if it exists
            except ValueError:
                # create new actor if it doesn't exist
                self._actor = DistributedSemaphoreActor.options(
                    name=self._name, namespace=self._namespace, lifetime="detached"
                ).remote(self._max_concurrent_ops)

    async def __aenter__(self):
        if self._actor is None:
            self._init_actor()
        await self._actor.acquire.remote()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._actor is None:
            self._init_actor()
        await self._actor.release.remote()

    def cleanup(self):
        if self._actor is None:
            self._init_actor()
        ray.get(self._actor.cleanup.remote())


def format_context(docs: list[Document], max_context_tokens: int = 4096) -> str:
    if not docs:
        return "No document found from the database"

    llm = ChatOpenAI(**config.llm)
    _length_function = llm.get_num_tokens

    docs_with_tokens = list(map(lambda d: (_length_function(d.page_content), d), docs))

    reduced_docs = []

    total_tokens = 0
    for n_tokens, doc in docs_with_tokens:
        if total_tokens + n_tokens > max_context_tokens:
            break
        reduced_docs.append(doc.page_content)
        total_tokens += n_tokens

    sep = "-" * 10 + "\n\n"
    logger.debug(
        "Context formatted", total_tokens=total_tokens, doc_count=len(reduced_docs)
    )
    return f"{sep}".join(reduced_docs)


# Initialize language detector
lang_detect_cache_dir = "/app/model_weights/"
lang_detector_config = LangDetectConfig(
    max_input_length=1024,  # chars
    model="auto",
    cache_dir=lang_detect_cache_dir,
)
lang_detector: LangDetector = LangDetector(config=lang_detector_config)


def detect_language(text: str):
    outputs = lang_detector.detect(text, k=1)
    return outputs[0].get("lang")


def get_llm_semaphore() -> DistributedSemaphore:
    return DistributedSemaphore(
        name="llmSemaphore",
        max_concurrent_ops=config.semaphore.llm_semaphore,
    )


def get_vlm_semaphore() -> DistributedSemaphore:
    return DistributedSemaphore(
        name="vlmSemaphore",
        max_concurrent_ops=config.semaphore.vlm_semaphore,
    )


get_llm_semaphore()
get_vlm_semaphore()
