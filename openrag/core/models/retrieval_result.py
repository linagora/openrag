"""Retrieval result domain models — per-chunk scored results."""

from __future__ import annotations

from typing import Any

from core.utils.consts import RETRIEVAL_SCORE_KEYS
from pydantic import BaseModel, Field

from .chunk import Chunk


class RetrievalResult(BaseModel):
    """A single scored chunk from retrieval."""

    chunk_id: str = ""
    document_id: str = ""
    text: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
    rerank_score: float | None = None
    page_number: int | None = None


class ScoredChunk(Chunk):
    """A :class:`Chunk` carrying the scores retrieval assigned it.

    A *subclass*, not a parallel DTO. Retrieval threads chunks through the
    retriever, expansion, RRF fusion and ``to_langchain()``, all typed
    ``list[Chunk]``; a flat sibling model would have to be converted back at
    every one of those boundaries, and the score would be dropped at whichever
    one got missed. Subclassing keeps those signatures honest while letting a
    scored chunk travel as itself.

    Every score defaults to ``None`` rather than ``0.0``: a stage that did not
    run (no reranker configured, or a chunk that reached the response without
    passing one) must be distinguishable from a stage that ran and scored the
    chunk zero.
    """

    vector_score: float | None = None
    rerank_score: float | None = None
    combined_score: float | None = None

    def to_langchain(self, *, with_id: bool = True) -> Any:
        """Same Document as :meth:`Chunk.to_langchain`, plus the scores.

        The scores ride in metadata because that is what reaches clients:
        API source entries are built from the chunk's metadata
        (``build_document_source_link``). A score that was never computed is
        omitted entirely rather than serialised as ``null``.

        Inherited metadata is cleared of score keys first, so the ones the
        Document carries are exactly the typed fields this retrieval set —
        never a value that rode in on ``metadata`` from somewhere else.
        """
        doc = super().to_langchain(with_id=with_id)
        for field in RETRIEVAL_SCORE_KEYS:
            doc.metadata.pop(field, None)
            value = getattr(self, field)
            if value is not None:
                doc.metadata[field] = value
        return doc

    @classmethod
    def from_chunk(cls, chunk: Chunk, **scores: float | None) -> ScoredChunk:
        """Re-key *chunk* as a ``ScoredChunk`` with *scores* applied.

        Accepts an already-scored chunk — the expansion path reranks a chunk a
        second time — and replaces just the named scores rather than nesting a
        second wrapper or resetting the ones it isn't given. Always returns a
        new object: the same ``Chunk`` is reranked once per sub-query on the
        multi-query path, so mutating in place would let one sub-query's score
        bleed into another's list.
        """
        if isinstance(chunk, cls):
            return chunk.model_copy(update=scores)
        return cls(**chunk.model_dump(), **scores)
