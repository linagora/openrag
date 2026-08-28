from __future__ import annotations

from collections import deque

import pytest


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ClaimConnection:
    def __init__(self, fetch_values):
        self.fetch_values = deque(fetch_values)
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchval(self, query: str, *params):
        self.executed.append((query, params))
        return self.fetch_values.popleft()

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "DELETE 0"


class _ClaimPool:
    def __init__(self, fetch_values):
        self.conn = _ClaimConnection(fetch_values)
        self.executed: list[tuple[str, tuple]] = []

    def acquire(self):
        return _AsyncContext(self.conn)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "DELETE 1"


@pytest.mark.asyncio
async def test_claim_content_hash_reserves_new_content() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool([None, "new-file"])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    conflict = await repo.claim_content_sha256(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )

    assert conflict is None
    assert any("INSERT INTO file_content_claims" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_claim_content_hash_returns_completed_duplicate() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool(["existing-file"])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    conflict = await repo.claim_content_sha256(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
        active_claim_tokens=set(),
    )

    assert conflict == "existing-file"
    assert not any("INSERT INTO file_content_claims" in query for query, _ in pool.conn.executed)
    assert not any("23 hours 59 minutes" in query for query, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_claim_content_hash_returns_active_duplicate() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool([None, None, "active-file"])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    conflict = await repo.claim_content_sha256(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )

    assert conflict == "active-file"


@pytest.mark.asyncio
async def test_claim_content_hash_recovers_old_claims_without_active_tasks() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool([None, "new-file"])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    conflict = await repo.claim_content_sha256(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-2",
        active_claim_tokens={"active-attempt"},
    )

    assert conflict is None
    query, params = next((query, params) for query, params in pool.conn.executed if "23 hours 59 minutes" in query)
    assert "claim_token = ANY($3::text[])" in query
    assert "claim_token NOT LIKE $4" in query
    assert params == ("tenant-a", "abc123", ["active-attempt"], "copy:%")


@pytest.mark.asyncio
async def test_replacement_can_claim_its_own_existing_content() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool(["same-file", "same-file"])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    conflict = await repo.claim_content_sha256(
        file_id="same-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
        replace=True,
    )

    assert conflict is None


@pytest.mark.asyncio
async def test_release_content_hash_claim_is_scoped_to_its_indexing_attempt() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool([])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    await repo.release_content_sha256_claim(
        file_id="new-file",
        partition="tenant-a",
        content_sha256="abc123",
        claim_token="attempt-1",
    )

    query, params = next(
        (query, params) for query, params in pool.conn.executed if "DELETE FROM file_content_claims" in query
    )
    assert "file_id = $3" in query
    assert "claim_token = $4" in query
    assert params == ("tenant-a", "abc123", "new-file", "attempt-1")


@pytest.mark.asyncio
async def test_renew_content_hash_claim_extends_only_its_owner() -> None:
    from services.persistence.document_repo import PgDocumentRepository

    pool = _ClaimPool([])
    repo = PgDocumentRepository(pool_getter=lambda: pool)

    assert (
        await repo.renew_content_sha256_claim(
            file_id="new-file",
            partition="tenant-a",
            content_sha256="abc123",
            claim_token="attempt-1",
        )
        is True
    )

    query, params = pool.executed[0]
    assert "SET expires_at" in query
    assert "file_id = $3" in query
    assert "claim_token = $4" in query
    assert params == ("tenant-a", "abc123", "new-file", "attempt-1")


def test_persistence_schema_enforces_partition_scoped_content_uniqueness() -> None:
    from services.persistence.schema import file_content_claims, files

    assert "content_sha256" in files.c
    index = next(index for index in files.indexes if index.name == "uix_files_partition_content_sha256")
    assert index.unique is True
    assert [column.name for column in index.columns] == ["partition_name", "content_sha256"]
    assert set(file_content_claims.primary_key.columns.keys()) == {"partition_name", "content_sha256"}
    assert file_content_claims.c.claim_token.nullable is False
