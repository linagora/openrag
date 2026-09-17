from __future__ import annotations

import pytest
from services.persistence.document_repo import PgDocumentRepository


class _Pool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *params):
        self.calls.append((query, params))
        return []


@pytest.mark.asyncio
async def test_degraded_stage_filter_runs_in_postgres_before_limit() -> None:
    pool = _Pool()
    repo = PgDocumentRepository(lambda: pool)

    await repo.list_partition_files("tenant-a", limit=25, degraded_stage="caption")

    query, params = pool.calls[0]
    assert "file_metadata::jsonb" in query
    assert "degraded_stages" in query
    assert query.index("degraded_stages") < query.index("LIMIT")
    assert params == ("tenant-a", "caption", 25)


@pytest.mark.asyncio
async def test_unfiltered_file_listing_keeps_the_existing_query_shape() -> None:
    pool = _Pool()
    repo = PgDocumentRepository(lambda: pool)

    await repo.list_partition_files("tenant-a", limit=25)

    query, params = pool.calls[0]
    assert "degraded_stages" not in query
    assert params == ("tenant-a", 25)
