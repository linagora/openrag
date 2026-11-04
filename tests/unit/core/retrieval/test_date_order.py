"""Unit tests for :mod:`core.retrieval.date_order`."""

from __future__ import annotations

from datetime import UTC, datetime

from core.models.chunk import Chunk
from core.retrieval.date_order import chunk_created_at, order_chunks_by_date


def _chunk(cid: str, created_at=None) -> Chunk:
    metadata = {} if created_at is None else {"created_at": created_at}
    return Chunk(id=cid, text=cid, metadata=metadata)


def _ids(chunks):
    return [c.id for c in chunks]


def test_none_order_is_the_identity():
    chunks = [_chunk("b", "2026-09-22T14:51:30Z"), _chunk("a", "2026-09-04T10:00:00Z")]
    assert order_chunks_by_date(chunks, None) is chunks


def test_newest_first_then_oldest_first():
    chunks = [
        _chunk("sep04", "2026-09-04T10:15:36.276759Z"),
        _chunk("sep22", "2026-09-22T14:51:30.786728Z"),
        _chunk("aug31", "2026-08-31T09:45:15.975481Z"),
    ]
    assert _ids(order_chunks_by_date(chunks, "newest")) == ["sep22", "sep04", "aug31"]
    assert _ids(order_chunks_by_date(chunks, "oldest")) == ["aug31", "sep04", "sep22"]


def test_chunks_of_one_document_keep_their_relevance_order():
    """Same ``created_at`` → the incoming (relevance) order is preserved, in
    both directions: a reverse sort must not flip the document's chunks."""
    chunks = [
        _chunk("old", "2026-09-04T10:00:00Z"),
        _chunk("new-best", "2026-09-22T14:51:30Z"),
        _chunk("new-second", "2026-09-22T14:51:30Z"),
        _chunk("new-third", "2026-09-22T14:51:30Z"),
    ]
    assert _ids(order_chunks_by_date(chunks, "newest")) == ["new-best", "new-second", "new-third", "old"]
    assert _ids(order_chunks_by_date(chunks, "oldest")) == ["old", "new-best", "new-second", "new-third"]


def test_undated_chunks_go_last_in_their_own_order_for_both_directions():
    """A file indexed before the client sent ``created_at`` has no date; it must
    never be taken for the newest (or the oldest) document."""
    chunks = [_chunk("u1"), _chunk("dated", "2026-09-22T14:51:30Z"), _chunk("u2", ""), _chunk("u3", "not a date")]
    assert _ids(order_chunks_by_date(chunks, "newest")) == ["dated", "u1", "u2", "u3"]
    assert _ids(order_chunks_by_date(chunks, "oldest")) == ["dated", "u1", "u2", "u3"]


def test_created_at_accepts_iso_strings_datetimes_and_reads_naive_as_utc():
    aware = datetime(2026, 9, 22, 14, 51, 30, tzinfo=UTC)
    assert chunk_created_at(_chunk("z", "2026-09-22T14:51:30Z")) == aware
    assert chunk_created_at(_chunk("offset", "2026-09-22T16:51:30+02:00")) == aware
    assert chunk_created_at(_chunk("naive", "2026-09-22T14:51:30")) == aware
    assert chunk_created_at(_chunk("dt", aware)) == aware
    assert chunk_created_at(_chunk("none")) is None
    assert chunk_created_at(_chunk("bad", "yesterday")) is None


def test_offsets_are_compared_as_instants_not_as_strings():
    """``16:00+02:00`` is 14:00 UTC: earlier than ``15:00Z`` even though the
    string sorts after it."""
    chunks = [_chunk("paris", "2026-09-22T16:00:00+02:00"), _chunk("utc", "2026-09-22T15:00:00Z")]
    assert _ids(order_chunks_by_date(chunks, "newest")) == ["utc", "paris"]
