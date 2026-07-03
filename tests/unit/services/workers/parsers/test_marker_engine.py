"""B1 — extracted Ray-free Marker chunk helpers (pure logic, no models/GPU)."""

from __future__ import annotations

from services.workers.parsers.marker_engine import MAX_PDF_PAGES, create_chunks


def test_single_chunk_when_within_size():
    assert create_chunks(5, 10) == [([0, 1, 2, 3, 4], "(5p)")]


def test_exact_size_is_single_chunk():
    assert create_chunks(10, 10) == [(list(range(10)), "(10p)")]


def test_multiple_chunks_cover_all_pages_contiguously():
    chunks = create_chunks(25, 10)
    ranges = [r for r, _ in chunks]
    labels = [lab for _, lab in chunks]

    assert ranges == [list(range(10)), list(range(10, 20)), list(range(20, 25))]
    assert labels == ["[p0-9]", "[p10-19]", "[p20-24]"]
    # full, gap-free coverage
    assert [p for r in ranges for p in r] == list(range(25))


def test_max_pdf_pages_constant_preserved():
    assert MAX_PDF_PAGES == 2000
