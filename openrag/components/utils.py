import asyncio
import threading
from typing import ClassVar

import ray
from config import load_config
from fast_langdetect import LangDetectConfig, LangDetector
from langchain_core.documents.base import Document
from utils.logger import get_logger

# Global variables
config = load_config()
logger = get_logger()


class SingletonMeta(type):
    _instances: ClassVar[dict] = {}
    _lock = threading.Lock()  # Ensures thread safety

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:  # First check (not thread-safe yet)
            with cls._lock:  # Prevents multiple threads from creating instances
                if cls not in cls._instances:  # Second check (double-checked locking)
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]


@ray.remote(max_restarts=5, max_concurrency=config.ray.semaphore.concurrency)
class DistributedSemaphoreActor:
    def __init__(self, max_concurrent_ops: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_ops)

    async def acquire(self):
        await self.semaphore.acquire()

    def release(self):
        self.semaphore.release()


class DistributedSemaphore:
    # https://chat.deepseek.com/a/chat/s/890dbcc0-2d3f-4819-af9d-774b892905bc
    def __init__(
        self,
        name: str = "llmSemaphore",
        namespace="openrag",
        max_concurrent_ops: int = 10,
    ):
        self._name = name
        self._namespace = namespace
        self._max_concurrent_ops = max_concurrent_ops

    def _get_or_create_actor(self):
        try:
            # reuse existing actor if it exists
            _actor = ray.get_actor(self._name, namespace=self._namespace)
        except ValueError:
            # create new actor if it doesn't exist
            _actor = DistributedSemaphoreActor.options(
                name=self._name,
                namespace=self._namespace,
                lifetime="detached",
            ).remote(self._max_concurrent_ops)
        except Exception:
            raise

        return _actor

    async def __aenter__(self):
        semaphore_actor = self._get_or_create_actor()
        await semaphore_actor.acquire.remote()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        semaphore_actor = self._get_or_create_actor()
        await semaphore_actor.release.remote()


def format_context(docs: list[Document], max_context_tokens: int = 4096) -> str:
    if not docs:
        return "No document found from the database"

    # Use a conservative character-based token estimator (~4 chars per token).
    # This avoids instantiating a model-specific tokenizer and works across
    # any LLM backend (GPT, Mistral, Llama, etc.) without undercounting.
    _chars_per_token = 4

    def _estimate_tokens(text: str) -> int:
        return max(1, len(text) // _chars_per_token)

    reduced_docs = []
    total_tokens = 0
    for doc in docs:
        n_tokens = _estimate_tokens(doc.page_content)
        if total_tokens + n_tokens > max_context_tokens:
            break
        reduced_docs.append(doc.page_content)
        total_tokens += n_tokens

    sep = "-" * 10 + "\n\n"
    logger.debug("Context formatted", total_tokens=total_tokens, doc_count=len(reduced_docs))
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


def get_audio_semaphore() -> DistributedSemaphore:
    return DistributedSemaphore(
        name="audioSemaphore",
        max_concurrent_ops=config.loader.transcriber.max_concurrent_chunks,
    )


get_llm_semaphore()
get_vlm_semaphore()
get_audio_semaphore()
