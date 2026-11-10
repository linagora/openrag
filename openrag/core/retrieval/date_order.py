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
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from typing import Any

from core.models.chunk import Chunk
from core.models.query import DateOrder

#: Chunk metadata key carrying the document's creation date (the one temporal
#: field the indexer accepts, see ``indexing_service.TEMPORAL_FIELDS``).
CREATED_AT_KEY = "created_at"

#: A ``created_at`` further ahead than this is upload metadata no clock produced;
#: the margin covers a client whose clock or timezone is off by hours.
FUTURE_TOLERANCE = timedelta(days=1)


def chunk_created_at(chunk: Chunk) -> datetime | None:
    """The chunk's document creation date, or ``None`` when absent, unparseable or in the future.

    Milvus returns the TIMESTAMPTZ column as an ISO 8601 string; a chunk built
    elsewhere may carry a ``datetime`` already. A naive value is read as UTC,
    matching the upload-time normalisation in ``extract_temporal_fields``. A
    future date would otherwise always sort as the newest document.
    """
    parsed = _parse_datetime(chunk.metadata.get(CREATED_AT_KEY))
    if parsed is not None and parsed > datetime.now(UTC) + FUTURE_TOLERANCE:
        return None
    return parsed


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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


def interleave_chunks(ranked_lists: Sequence[Sequence[Chunk]], key_fn: Callable[[Chunk], Hashable]) -> list[Chunk]:
    """Take from each list in turn, skipping repeats, so every list's top comes first."""
    merged: dict[Hashable, Chunk] = {}
    for row in zip_longest(*ranked_lists):
        for chunk in row:
            if chunk is not None:
                merged.setdefault(key_fn(chunk), chunk)
    return list(merged.values())


__all__ = ["CREATED_AT_KEY", "FUTURE_TOLERANCE", "chunk_created_at", "interleave_chunks", "order_chunks_by_date"]
