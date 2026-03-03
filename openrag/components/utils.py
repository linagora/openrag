import asyncio
import copy
import json
import re
import threading
from collections import deque
from typing import ClassVar

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


_cached_length_function = None


def get_num_tokens():
    global _cached_length_function
    if _cached_length_function is None:
        llm = ChatOpenAI(**config.llm)
        _cached_length_function = llm.get_num_tokens
    return _cached_length_function


def format_context(
    docs: list[Document], max_context_tokens: int = 4096, number_sources: bool = True
) -> tuple[str, list[int]]:
    if not docs:
        return "No document found from the database", []

    _length_function = get_num_tokens()

    reduced_docs = []
    included_indices = []
    total_tokens = 0

    for i, doc in enumerate(docs):
        prefix = f"[Source {len(reduced_docs) + 1}]\n" if number_sources else ""
        n_tokens = _length_function(doc.page_content)
        if prefix:
            n_tokens += _length_function(prefix)
        if total_tokens + n_tokens > max_context_tokens:
            break
        reduced_docs.append(f"{prefix}{doc.page_content}")
        included_indices.append(i)
        total_tokens += n_tokens

    sep = "-" * 10 + "\n\n"
    logger.debug("Context formatted", total_tokens=total_tokens, doc_count=len(reduced_docs))
    return f"{sep}".join(reduced_docs), included_indices


_SOURCES_NONE_RE = re.compile(r"\n?\[?Sources?\]?\s*:\s*\[?\s*none\s*\]?\s*$", re.IGNORECASE)
_SOURCES_NUMS_RE = re.compile(r"\n?\[?Sources?\]?\s*:\s*\[?([\d,\s]+)\]?[.\s]*$")


def extract_and_strip_sources_block(text: str) -> tuple[str, set[int] | None]:
    """Extract [Sources: ...] block from end of response. Return (clean_text, citations).

    Returns:
        (clean_text, citations) where citations is:
        - set of ints: LLM cited specific sources
        - empty set:   LLM explicitly said [Sources: none]
        - None:        LLM didn't include any sources tag
    """
    # Check for explicit "none" first
    match_none = _SOURCES_NONE_RE.search(text)
    if match_none:
        clean_text = text[: match_none.start()].rstrip()
        logger.debug("LLM explicitly reported no sources used")
        return clean_text, set()

    # Check for numbered citations
    match = _SOURCES_NUMS_RE.search(text)
    if not match:
        tail = text[-150:] if len(text) > 150 else text
        logger.debug("No [Sources: ...] tag found in LLM response", tail=repr(tail))
        return text, None

    citations = {int(n.strip()) for n in match.group(1).split(",") if n.strip().isdigit()}
    logger.debug(
        "Extracted source citations from LLM response", citations=sorted(citations), matched=repr(match.group(0))
    )
    clean_text = text[: match.start()].rstrip()
    return clean_text, citations


def filter_sources_by_citations(sources: list, citations: set[int] | None) -> list:
    """Keep only sources whose 1-based index was cited.

    - citations is None:      LLM didn't include tag → fallback to all sources
    - citations is empty set:  LLM said [Sources: none] → return no sources
    - citations has values:    filter to cited sources only
    """
    if citations is None:
        return sources
    if not citations:
        return []
    filtered = [s for i, s in enumerate(sources, start=1) if i in citations]
    return filtered if filtered else sources


async def stream_with_source_filtering(
    llm_stream,
    sources: list,
    model_name: str,
    buffer_size: int = 100,
):
    """Process an LLM SSE stream, stripping the [Sources: ...] tag from content.

    Buffers the last `buffer_size` chars of content to intercept the sources tag
    before it reaches the client. On stream end, strips the tag and emits a finish
    chunk with filtered source metadata.

    Yields SSE "data: ..." lines ready to forward to the client.
    """
    chunk_buffer: deque[dict] = deque()
    buffered_content_len = 0
    last_chunk_template = None
    last_finish_reason = None

    async for line in llm_stream:
        if not line.startswith("data:"):
            continue

        if line.strip() == "data: [DONE]":
            buffered_text = "".join(
                (c.get("choices", [{}])[0].get("delta", {}).get("content", "") or "") for c in chunk_buffer
            )
            clean_text, citations = extract_and_strip_sources_block(buffered_text)
            filtered = filter_sources_by_citations(sources, citations)
            filtered_json = json.dumps({"sources": filtered})

            remaining = clean_text
            for chunk in chunk_buffer:
                original_content = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "") or ""
                if not remaining:
                    break
                surviving = remaining[: len(original_content)]
                remaining = remaining[len(original_content) :]
                if surviving != original_content:
                    chunk["choices"][0]["delta"]["content"] = surviving
                chunk["extra"] = filtered_json
                yield f"data: {json.dumps(chunk)}\n\n"

            if last_chunk_template:
                # FIXME: race condition where clients missed sources because finish_reason
                # were received before sources
                await asyncio.sleep(0.05)
                finish_chunk = copy.deepcopy(last_chunk_template)
                finish_chunk["choices"][0]["delta"] = {}
                finish_chunk["choices"][0]["finish_reason"] = last_finish_reason or "stop"
                finish_chunk["extra"] = filtered_json
                yield f"data: {json.dumps(finish_chunk)}\n\n"

            yield "data: [DONE]\n\n"

        else:
            data_str = line[len("data: ") :]
            data = json.loads(data_str)
            data["model"] = model_name

            choice = data.get("choices", [{}])[0]
            delta = choice.get("delta", {})
            content = delta.get("content", "") or ""
            finish_reason = choice.get("finish_reason")

            if finish_reason:
                # Save finish_reason, don't forward — we emit it at the end
                last_finish_reason = finish_reason
                last_chunk_template = data
            elif content:
                last_chunk_template = data
                chunk_buffer.append(data)
                buffered_content_len += len(content)

                while buffered_content_len > buffer_size:
                    oldest = chunk_buffer.popleft()
                    oldest_content = oldest.get("choices", [{}])[0].get("delta", {}).get("content", "") or ""
                    oldest["extra"] = "{}"
                    buffered_content_len -= len(oldest_content)
                    yield f"data: {json.dumps(oldest)}\n\n"

            else:
                # Forward non-content, non-finish chunks immediately (e.g. role delta)
                data["extra"] = "{}"
                yield f"data: {json.dumps(data)}\n\n"


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
