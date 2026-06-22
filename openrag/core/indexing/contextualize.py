"""Chunk contextualization against the ``LLM`` ABC.

Framework-free implementation of contextual retrieval: for each chunk,
ask an LLM to write a short situating context based on the document's
opening chunks plus the immediate preceding neighbourhood, then prepend
that context to the chunk text so embeddings capture document-level
meaning.

Inputs and outputs are :class:`core.models.chunk.Chunk` instances. The
caller supplies the LLM, the system prompt, and any concurrency / timeout
limits — core does not reach into Hydra config or the global VLM
semaphore.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager, nullcontext

from tqdm.asyncio import tqdm

from ..llm import LLM
from ..models.chunk import Chunk
from ..prompts.contextualization_builder import build_messages, wrap_chunk_with_context

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_BATCH_SIZE = 4


class ChunkContextualizer:
    """Generate a per-chunk context string and prepend it to the chunk text.."""

    def __init__(
        self,
        llm: LLM,
        system_prompt: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        llm_semaphore: AbstractAsyncContextManager[object] | None = None,
    ):
        self._llm = llm
        self._system_prompt = system_prompt
        self._timeout = timeout_seconds
        self._batch_size = max(1, batch_size)
        # Optional cluster-wide LLM gate, injected by the caller (e.g. the Ray
        # "llmSemaphore"); wraps each chat call so contextualization shares the
        # global LLM concurrency budget. No-op when not supplied.
        self._semaphore = llm_semaphore or nullcontext()

    async def _generate_context(
        self,
        first_chunks: Sequence[Chunk],
        prev_chunks: Sequence[Chunk],
        current_chunk: Chunk,
        filename: str,
        lang: str,
    ) -> str:
        messages = build_messages(
            system_prompt=self._system_prompt,
            filename=filename,
            first_chunks_text=[c.text for c in first_chunks],
            prev_chunks_text=[c.text for c in prev_chunks],
            current_chunk_text=current_chunk.text,
            lang=lang,
        )
        async with self._semaphore:
            try:
                response = await asyncio.wait_for(self._llm.chat(messages), timeout=self._timeout)
                return _chat_response_text(response)
            except TimeoutError:
                logger.warning("LLM timeout contextualizing chunk (filename=%s)", filename)
                return ""
            except Exception as exc:
                logger.warning("Error contextualizing chunk (filename=%s): %s", filename, exc)
                return ""

    async def contextualize(
        self,
        chunks: Sequence[Chunk],
        *,
        filename: str = "",
        lang: str = "en",
    ) -> list[Chunk]:
        """Return new chunks with context prepended to ``text``.

        Each returned chunk preserves the input's id, metadata, and other
        fields; ``text`` is rewritten to the formatted (context + content)
        string used for embedding, ``context`` holds the generated context,
        and ``content`` holds the original chunk text.

        Falls back to returning the input chunks unchanged on any
        unrecoverable error.
        """
        chunks = list(chunks)
        if not chunks:
            return []

        try:
            first_chunks = chunks[:2]
            contexts: list[str] = []
            # Schedule one batch at a time so prompt strings + coroutine
            # objects don't all sit in memory upfront on large documents.
            for start in range(0, len(chunks), self._batch_size):
                end = min(start + self._batch_size, len(chunks))
                batch = [
                    self._generate_context(
                        first_chunks=first_chunks,
                        prev_chunks=chunks[max(0, i - 2) : i] if i > 0 else [],
                        current_chunk=chunks[i],
                        filename=filename,
                        lang=lang,
                    )
                    for i in range(start, end)
                ]
                contexts.extend(
                    await tqdm.gather(
                        *batch,
                        desc=f"Contextualizing chunks of *{filename}* [{start + 1}-{end}/{len(chunks)}]",
                    )
                )

            return [
                chunk.model_copy(
                    update={
                        "text": wrap_chunk_with_context(
                            content=chunk.text,
                            filename=filename,
                            chunk_context=context,
                        ),
                        "context": context,
                        "content": chunk.text,
                    }
                )
                for chunk, context in zip(chunks, contexts, strict=True)
            ]
        except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Error contextualizing chunks from %s: %s", filename, exc)
            return chunks


def _chat_response_text(response: object) -> str:
    """Extract the assistant text from an LLM ``chat`` result.

    Accepts a raw string or an OpenAI-style response dict
    (``choices[0].message.content``, ``choices[0].text``, or a top-level
    ``content``); returns ``""`` for any unrecognized shape.
    """
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]

    content = response.get("content")
    return content if isinstance(content, str) else ""
