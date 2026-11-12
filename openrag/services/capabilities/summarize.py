"""The ``summarize`` step: map-reduce over the retrieved chunks.

Each chunk is sent to the LLM with the query; the ones it finds relevant are
kept, as its summary of them. Chunks are judged in batches, and the run stops
early once a whole batch is irrelevant (``map_reduce`` in the config).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

from core.prompts import build_map_messages
from core.utils.logging import get_logger
from core.utils.string_utils import json_object_slice

if TYPE_CHECKING:
    from core.config.root import Settings
    from core.llm.llm import LLM

logger = get_logger()


class Summarizer:
    """Keeps the chunks relevant to a query, replaced by their summaries.

    *semaphore* bounds the concurrent LLM calls, shared with the rest of the
    process.
    """

    def __init__(
        self,
        *,
        llm: LLM,
        config: Settings,
        semaphore: Callable[[], AbstractAsyncContextManager],
    ) -> None:
        # The static settings.llm client, not the endpoint that answers the
        # request: a map-reduce request's sub-calls may run on another model
        # than its answer until they go through the resolved chat_llm too.
        self._llm = llm
        self._semaphore = semaphore
        mr = config.map_reduce
        self.initial_batch_size = mr.initial_batch_size
        self.expansion_batch_size = mr.expansion_batch_size
        self.max_total_documents = mr.max_total_documents

    async def _infer_relevancy(self, query: str, doc) -> tuple[bool, str]:
        async with self._semaphore():
            try:
                resp = await self._llm.chat(
                    build_map_messages(query=query, content=doc.page_content),
                    max_tokens=512,
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
                data = json.loads(json_object_slice(resp["choices"][0]["message"]["content"]))
                return bool(data.get("relevancy", False)), str(data.get("summary", "") or "")
            except Exception as e:
                logger.error("Error during chunk relevancy inference", error=str(e))
                return False, ""

    async def summarize(self, query: str, docs: list) -> list:
        """The relevant *docs*, each as its summary, at most ``max_total_documents`` of them."""

        async def _batch(chunks: list, summaries: list) -> bool:
            outputs = await asyncio.gather(*[self._infer_relevancy(query, c) for c in chunks])
            terminate = all(not rel for rel, _ in outputs[-self.expansion_batch_size :])
            for (rel, summary), chunk in zip(outputs, chunks, strict=True):
                if rel:
                    summaries.append(_summary_doc(chunk, summary))
            return terminate

        summaries: list = []
        initial, remaining = docs[: self.initial_batch_size], docs[self.initial_batch_size :]
        terminate = await _batch(initial, summaries)
        if terminate or not remaining or len(summaries) >= self.max_total_documents:
            return summaries

        for i in range(0, len(remaining), self.expansion_batch_size):
            n = min(self.expansion_batch_size, self.max_total_documents - len(summaries))
            if n <= 0:
                break
            terminate = await _batch(remaining[i : i + n], summaries)
            if terminate or len(summaries) >= self.max_total_documents:
                break
        logger.debug("Map reduce completed", relevant_chunks_count=len(summaries))
        return summaries


def _summary_doc(chunk, summary: str):
    """A summarised copy of a LangChain Document (page_content replaced)."""
    return chunk.__class__(page_content=summary, metadata=chunk.metadata)
