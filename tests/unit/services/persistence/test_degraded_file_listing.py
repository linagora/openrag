from __future__ import annotations

from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_file_metadata_lookup_is_partition_scoped() -> None:
    pool = AsyncMock()
    pool.fetchval.return_value = {"title": "Report", "degraded_stages": ["caption"]}
    repo = PgDocumentRepository(lambda: pool)

    metadata = await repo.get_file_metadata("file-1", "tenant-a")

    assert metadata == {"title": "Report", "degraded_stages": ["caption"]}
    query, file_id, partition = pool.fetchval.call_args.args
    assert "file_metadata" in query
    assert "file_id = $1" in query
    assert "partition_name = $2" in query
    assert (file_id, partition) == ("file-1", "tenant-a")


@pytest.mark.asyncio
async def test_metadata_update_atomically_merges_patch_and_protects_degradation() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 1"
    repo = PgDocumentRepository(lambda: pool)

    updated = await repo.update_file_metadata_in_db(
        "file-1",
        "tenant-a",
        {"title": "new", "degraded_stages": []},
    )

    assert updated is True
    query, patch, file_id, partition = pool.execute.call_args.args
    assert "COALESCE(file_metadata::jsonb" in query
    assert "|| $1::jsonb" in query
    assert patch == {"title": "new"}
    assert (file_id, partition) == ("file-1", "tenant-a")
