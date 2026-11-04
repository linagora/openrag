"""Reorder retrieved chunks by their document's creation date.

Retrieval ranks by relevance (similarity, reranker, RRF). A request such as
"summarize the last meeting I had" is not answered by the most *similar*
meeting but by the most *recent* one, and the answering LLM only sees the
sources that fit its token budget, in order. So when the query contextualizer
flags a request as ``date_order="newest"`` / ``"oldest"``, the relevance-ranked
list is reordered here before the budget cut, so the document the user means
is the first thing the model reads.

The sort is stable: chunks of one document share a ``created_at`` and therefore
keep their relevance order relative to each other, and chunks without a date
(files indexed before the client sent ``created_at``) keep theirs at the tail —
an unknown date is never mistaken for the newest or the oldest.

A date only decides between documents that are about the right thing, so
``drop_below_relevance_floor`` removes the chunks the reranker scored far below
the best match before a pipeline sorts its list.
"""

from __future__ import annotations

from datetime import datetime

from core.models.chunk import Chunk
from core.models.query import DateOrder
from core.utils.dates import parse_datetime

#: Chunk metadata key carrying the document's creation date (the one temporal
#: field the indexer accepts, see ``indexing_service.TEMPORAL_FIELDS``).
CREATED_AT_KEY = "created_at"

#: A chunk scoring under this share of the best rerank score is not a candidate
#: for "the last X". A ratio rather than an absolute value because the score's
#: scale depends on the reranker provider.
RELEVANCE_FLOOR_RATIO = 0.5


def chunk_created_at(chunk: Chunk) -> datetime | None:
    """The chunk's document creation date, or ``None`` when absent or unparseable.

    Milvus returns the TIMESTAMPTZ column as an ISO 8601 string; a chunk built
    elsewhere may carry a ``datetime`` already.
    """
    return parse_datetime(chunk.metadata.get(CREATED_AT_KEY))


def drop_below_relevance_floor(chunks: list[Chunk]) -> list[Chunk]:
    """``chunks`` without those scoring under ``RELEVANCE_FLOOR_RATIO`` of the best.

    Without it, a newer but barely related document outranks the one the user
    means, whatever its score. Identity when nothing can be compared: no reranker
    ran, or the best score isn't positive (a ratio means nothing on raw logits).
    """
    scores = [score for chunk in chunks if (score := getattr(chunk, "rerank_score", None)) is not None]
    if not scores or max(scores) <= 0:
        return chunks
    floor = max(scores) * RELEVANCE_FLOOR_RATIO
    return [chunk for chunk in chunks if (getattr(chunk, "rerank_score", None) or 0) >= floor]


def order_chunks_by_date(chunks: list[Chunk], order: DateOrder | None) -> list[Chunk]:
    """Return ``chunks`` reordered by document date; ``order=None`` is the identity.

    Dated chunks come first, newest or oldest first per ``order``; undated
    chunks follow in their incoming order. Stable within equal dates.
    """
    if not order or not chunks:
        return chunks
    dated: list[tuple[datetime, int, Chunk]] = []
    undated: list[Chunk] = []
    for position, chunk in enumerate(chunks):
        stamp = chunk_created_at(chunk)
        if stamp is None:
            undated.append(chunk)
        else:
            dated.append((stamp, position, chunk))
    if order == "newest":
        # A plain reverse sort would also flip the relevance order of chunks
        # sharing a date, so the position is part of the key.
        dated.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    else:
        dated.sort(key=lambda item: (item[0], item[1]))
    return [chunk for _, _, chunk in dated] + undated


__all__ = [
    "CREATED_AT_KEY",
    "RELEVANCE_FLOOR_RATIO",
    "chunk_created_at",
    "drop_below_relevance_floor",
    "order_chunks_by_date",
]
