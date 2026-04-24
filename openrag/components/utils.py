import asyncio
import copy
import json
import re
import threading
from typing import ClassVar

import ray
from components.indexer.utils.text_sanitizer import sanitize_text
from config import load_config
from fast_langdetect import LangDetectConfig, LangDetector
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from utils.logger import get_logger

SOURCE_SEPARATOR = "-" * 10 + "\n\n"

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
        llm = ChatOpenAI(**config.llm.model_dump())
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

    logger.debug("Context formatted", total_tokens=total_tokens, doc_count=len(reduced_docs))
    return SOURCE_SEPARATOR.join(reduced_docs), included_indices


def format_web_context(
    web_results: list,
    start_index: int = 1,
    max_tokens: int = 2000,
) -> tuple[str, list[int], int]:
    """Format web results as numbered [Source N] blocks within a token budget.

    Uses fetched page content when available, falling back to the search snippet.

    Args:
        web_results: Results from web search provider (list of WebResult)
        start_index: First source number (continues numbering after RAG sources)
        max_tokens: Maximum token budget for all web sources combined

    Returns:
        (formatted_string, list_of_source_numbers_used, total_tokens_used)
    """
    if not web_results:
        return "", [], 0

    _length_function = get_num_tokens()

    parts = []
    source_numbers = []
    total_tokens = 0

    for i, result in enumerate(web_results):
        n = start_index + i
        title = sanitize_text(result.title)
        body = sanitize_text(result.content) if result.content else sanitize_text(result.snippet)
        block = f"[Source {n}]\n{title}\n{body}"
        block_tokens = _length_function(block)
        if total_tokens + block_tokens > max_tokens and parts:
            break
        parts.append(block)
        source_numbers.append(n)
        total_tokens += block_tokens

    logger.debug("Web context formatted", total_tokens=total_tokens, source_count=len(parts))
    return SOURCE_SEPARATOR.join(parts), source_numbers, total_tokens


# Line-terminal anchor `(?=\n|$)` — matches only when the tag sits flush against
# a newline or end-of-string. Safe: in-prose tags like "use [Sources: 1, 3] at end"
# are followed by text, so they stay. The LLM always places misplaced tags at the
# end of a sentence/bullet/line, which is exactly what this catches.
_SOURCES_NONE_RE = re.compile(
    r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?\s*none\s*\]?[.\s]*?(?=\n|$)",
    re.IGNORECASE,
)
_SOURCES_NUMS_RE = re.compile(r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?([\d,\s]+)\]?[.\s]*?(?=\n|$)")


def _strip_sources_tags(text: str) -> tuple[str, set[int], bool]:
    """Strip every line-terminal [Sources: ...] tag. Return (cleaned, cited_nums, saw_none)."""
    cited: set[int] = set()
    for m in _SOURCES_NUMS_RE.finditer(text):
        cited.update(int(n.strip()) for n in m.group(1).split(",") if n.strip().isdigit())
    saw_none = bool(_SOURCES_NONE_RE.search(text))
    cleaned = _SOURCES_NUMS_RE.sub("", text)
    cleaned = _SOURCES_NONE_RE.sub("", cleaned)
    return cleaned, cited, saw_none


def extract_and_strip_sources_block(text: str) -> tuple[str, set[int] | None]:
    """Strip every line-terminal [Sources: ...] tag and return merged citations.

    Returns:
        (clean_text, citations) where citations is:
        - set of ints: union of all cited source numbers across every tag occurrence
        - empty set:   LLM said [Sources: none] and no numeric citations elsewhere
        - None:        no sources tag found — text returned unchanged
    """
    cleaned, citations, saw_none = _strip_sources_tags(text)

    if not citations and not saw_none:
        tail = text[-150:] if len(text) > 150 else text
        logger.debug("No [Sources: ...] tag found in LLM response", tail=repr(tail))
        return text, None

    cleaned = cleaned.rstrip()
    if citations:
        logger.debug("Extracted source citations from LLM response", citations=sorted(citations))
        return cleaned, citations

    logger.debug("LLM explicitly reported no sources used")
    return cleaned, set()


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


# Look-ahead window: chars held back at the tail of `pending` so an in-flight
# [Sources: ...] tag can never straddle the emit boundary. Sized for the longest
# plausible tag (~60 chars) plus margin for spacing variations.
_STREAM_LOOKAHEAD = 80


async def stream_with_source_filtering(
    llm_stream,
    sources: list,
    model_name: str,
):
    """Process an LLM SSE stream, stripping every line-terminal [Sources: ...] tag.

    Look-ahead window: keep the last `_STREAM_LOOKAHEAD` chars of `pending`
    buffered and emit everything before that. The held-back tail guarantees no
    in-flight tag can straddle the emit boundary, so streaming flows in
    real-time (constant ~80-char content lag) regardless of newline cadence.
    On stream end, flush the tail (EOS-anchored regex catches a final tag
    without a trailing \n) and emit a finish chunk carrying the filtered
    source metadata.

    Yields SSE "data: ..." lines ready to forward to the client.
    """
    pending = ""
    emitted_len = 0
    chunk_template = None
    last_finish_reason = None

    async for line in llm_stream:
        if not line.startswith("data:"):
            continue

        if line.strip() == "data: [DONE]":
            final_clean, citations = extract_and_strip_sources_block(pending)
            final_clean = final_clean.rstrip()

            filtered = filter_sources_by_citations(sources, citations)
            filtered_json = json.dumps({"sources": filtered})

            if chunk_template and len(final_clean) > emitted_len:
                tail_chunk = copy.deepcopy(chunk_template)
                tail_chunk["choices"][0]["delta"] = {"content": final_clean[emitted_len:]}
                tail_chunk["extra"] = filtered_json
                yield f"data: {json.dumps(tail_chunk)}\n\n"

            if chunk_template:
                # FIXME: race condition where clients miss sources because finish_reason
                # arrives before the sources metadata
                await asyncio.sleep(0.05)
                finish_chunk = copy.deepcopy(chunk_template)
                finish_chunk["choices"][0]["delta"] = {}
                finish_chunk["choices"][0]["finish_reason"] = last_finish_reason or "stop"
                finish_chunk["extra"] = filtered_json
                yield f"data: {json.dumps(finish_chunk)}\n\n"

            yield "data: [DONE]\n\n"
            continue

        data = json.loads(line[len("data: ") :])
        data["model"] = model_name

        choice = data.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        content = delta.get("content", "") or ""
        finish_reason = choice.get("finish_reason")

        if finish_reason:
            # Save finish_reason, don't forward — we emit it at the end
            last_finish_reason = finish_reason
            chunk_template = data
        elif content:
            chunk_template = data
            pending += content

            if len(pending) <= _STREAM_LOOKAHEAD:
                continue

            # Strip tags from the whole pending; emit the prefix that lies safely
            # outside the look-ahead window. Tags inside the window stay buffered
            # until they're either confirmed (anchored by \n) or completed at DONE.
            cleaned, _, _ = _strip_sources_tags(pending)
            safe_end = max(0, len(cleaned) - _STREAM_LOOKAHEAD)
            if safe_end > emitted_len:
                # Shallow rebuild: data is fresh from json.loads (no aliasing),
                # so we only need to avoid mutating shared inner dicts.
                choice = data["choices"][0]
                out = {
                    **data,
                    "choices": [
                        {**choice, "delta": {**choice.get("delta", {}), "content": cleaned[emitted_len:safe_end]}}
                    ],
                    "extra": "{}",
                }
                yield f"data: {json.dumps(out)}\n\n"
                emitted_len = safe_end
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
