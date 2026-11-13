"""Unit tests for ``PgDocumentRepository.list_catalog_files``: what a file listing reads."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from core.models.catalog import CatalogFile
from services.persistence.document_repo import PgDocumentRepository

_ROW = {
    "partition_name": "hr",
    "file_id": "f1",
    "indexed_at": datetime(2026, 11, 12, 17, 6, tzinfo=UTC),
    "chunk_count": 169,
    "filename": "report.pdf",
    "original_filename": "Report.pdf",
    "mimetype": "application/pdf",
    "file_size": "1.84 MB",
    "created_at": None,
}


class _Pool:
    def __init__(self, rows=()) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self._rows = list(rows)

    async def fetch(self, query: str, *params):
        self.calls.append((query, params))
        return self._rows


@pytest.mark.asyncio
async def test_a_listing_reads_named_fields_only_newest_first():
    pool = _Pool([_ROW])
    repo = PgDocumentRepository(lambda: pool)

    files = await repo.list_catalog_files(("hr", "legal"), file_ids=["f1"], limit=5)

    query, params = pool.calls[0]
    assert "SELECT *" not in query
    assert "file_metadata->>'filename'" in query
    assert "ORDER BY indexed_at DESC" in query
    assert params == (["hr", "legal"], ["f1"], 5)
    assert files == [
        CatalogFile(
            partition="hr",
            file_id="f1",
            filename="report.pdf",
            original_filename="Report.pdf",
            mimetype="application/pdf",
            file_size="1.84 MB",
            created_at=None,
            indexed_at=_ROW["indexed_at"],
            chunk_count=169,
        )
    ]


@pytest.mark.asyncio
async def test_no_partitions_or_files_means_no_restriction():
    pool = _Pool()
    repo = PgDocumentRepository(lambda: pool)

    await repo.list_catalog_files(None, limit=10)

    assert pool.calls[0][1] == (None, None, 10)


@pytest.mark.asyncio
async def test_a_listing_needs_a_limit():
    repo = PgDocumentRepository(lambda: _Pool())

    with pytest.raises(ValueError, match="limit"):
        await repo.list_catalog_files(None, limit=0)
