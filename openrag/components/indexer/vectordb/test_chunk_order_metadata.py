"""Regression test for #356 — concurrent chunk section IDs must not collide.

The previous implementation seeded every call with ``time.time_ns()`` and
incremented by ``i``. Two ingestions starting within the same nanosecond
produced overlapping integer ranges, corrupting prev/next_section_id
navigation across documents. The fix replaces the seed with a
cryptographically random 60-bit base.
"""

from components.indexer.vectordb.vectordb import _gen_chunk_order_metadata


def test_two_concurrent_calls_have_disjoint_ranges():
    a = _gen_chunk_order_metadata(n=200)
    b = _gen_chunk_order_metadata(n=200)
    ids_a = {row["section_id"] for row in a}
    ids_b = {row["section_id"] for row in b}
    assert ids_a.isdisjoint(ids_b)


def test_ids_fit_in_int64():
    rows = _gen_chunk_order_metadata(n=10_000)
    int64_max = 2**63 - 1
    for row in rows:
        assert 0 <= row["section_id"] < int64_max


def test_prev_next_chain_is_consistent_within_call():
    rows = _gen_chunk_order_metadata(n=5)
    section_ids = [row["section_id"] for row in rows]
    # prev/next must reference adjacent neighbours from the same call
    for i, row in enumerate(rows):
        if i == 0:
            assert row["prev_section_id"] is None
        else:
            assert row["prev_section_id"] == section_ids[i - 1]
        if i == len(rows) - 1:
            assert row["next_section_id"] is None
        else:
            assert row["next_section_id"] == section_ids[i + 1]
