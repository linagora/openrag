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

from ..llm import LLM
from ..models.chunk import Chunk

logger = logging.getLogger(__name__)


BASE_CHUNK_FORMAT = "* filename: {filename}\n\n[CHUNK_START]\n\n{content}\n\n[CHUNK_END]"
CHUNK_FORMAT = "[CONTEXT]\n\n{chunk_context}\n\n" + BASE_CHUNK_FORMAT

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CONCURRENT = 4


def format_chunk(content: str, filename: str, chunk_context: str | None = None) -> str:
    """Render the canonical chunk wrapping (with or without context)."""
    if chunk_context:
        return CHUNK_FORMAT.format(content=content, filename=filename, chunk_context=chunk_context)
    return BASE_CHUNK_FORMAT.format(content=content, filename=filename)


class ChunkContextualizer:
    """Generate a per-chunk context string and prepend it to the chunk text."""

    def __init__(
        self,
        llm: LLM,
        system_prompt: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        semaphore: asyncio.Semaphore | None = None,
    ):
        self._llm = llm
        self._system_prompt = system_prompt
        self._timeout = timeout_seconds
        self._semaphore = semaphore or asyncio.Semaphore(max_concurrent)

    async def _generate_context(
        self,
        first_chunks: Sequence[Chunk],
        prev_chunks: Sequence[Chunk],
        current_chunk: Chunk,
        filename: str,
        lang: str,
    ) -> str:
        first_block = "\n--\n".join(c.text for c in first_chunks)
        prev_block = "\n--\n".join(c.text for c in prev_chunks)
        user_msg = (
            "Here is the context to consider for generating the context:\n"
            f"- Filename: {filename}\n"
            f"- First chunks:\n{first_block}\n\n"
            f"- Previous chunks:\n{prev_block}\n\n"
            f"Here is the current chunk to contextualize strictly in this {lang} language:\n"
            f"- Current chunk:\n\n{current_chunk.text}"
        )
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": user_msg},
        ]
        async with self._semaphore:
            try:
                return await asyncio.wait_for(self._llm.chat(messages), timeout=self._timeout)
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
            tasks = [
                self._generate_context(
                    first_chunks=first_chunks,
                    prev_chunks=chunks[max(0, i - 2) : i] if i > 0 else [],
                    current_chunk=chunks[i],
                    filename=filename,
                    lang=lang,
                )
                for i in range(len(chunks))
            ]
            contexts = await asyncio.gather(*tasks)

            return [
                chunk.model_copy(
                    update={
                        "text": format_chunk(
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
        except Exception as exc:
            logger.warning("Error contextualizing chunks from %s: %s", filename, exc)
            return chunks
