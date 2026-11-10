"""Order the sources shown to the answering LLM by their document's creation date.

A request such as "summarize the last meeting I had" is answered by the most
*recent* meeting, not the most similar one. The date only decides the order the
model reads the sources in, never which sources it gets: retrieval, the
``top_k`` cut and the context-budget fit all stay relevance-ranked, so the
model sees what it would without a date order, newest (or oldest) first and
each with its ``Created:`` line to pick from. Letting the date select sources
instead lets recent, loosely matching documents (invitations, agendas) crowd
out the one asked for.

The sort is stable: chunks of one document share a ``created_at`` and therefore
keep their relevance order relative to each other, and chunks without a date
(files indexed before the client sent ``created_at``) keep theirs at the tail —
an unknown date is never mistaken for the newest or the oldest.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from core.models.query import DateOrder


class _HasMetadata(Protocol):
    """A ``Chunk`` or the LangChain ``Document`` built from it."""

    metadata: dict


#: Chunk metadata key carrying the document's creation date (the one temporal
#: field the indexer accepts, see ``indexing_service.TEMPORAL_FIELDS``).
CREATED_AT_KEY = "created_at"

#: A ``created_at`` further ahead than this is upload metadata no clock produced;
#: the margin covers a client whose clock or timezone is off by hours.
FUTURE_TOLERANCE = timedelta(days=1)


def chunk_created_at(chunk: _HasMetadata) -> datetime | None:
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


def order_chunks_by_date[T: _HasMetadata](chunks: Sequence[T], order: DateOrder | None) -> list[T]:
    """Return ``chunks`` reordered by document date; ``order=None`` is the identity.

    Dated chunks come first, newest or oldest first per ``order``; undated
    chunks follow in their incoming order. Stable within equal dates.
    """
    if not order or not chunks:
        return list(chunks)
    dated: list[tuple[datetime, int, T]] = []
    undated: list[T] = []
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


__all__ = ["CREATED_AT_KEY", "FUTURE_TOLERANCE", "chunk_created_at", "order_chunks_by_date"]
