"""Unit tests for PgPresetRepository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_row(**kwargs):
    base = {
        "name": "default",
        "preset_type": "indexation",
        "config": {"chunk_size": 512},
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kwargs)
    return base


class _FakePool:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchrow_result = None
        self._fetch_result: list = []
        self._fetchval_result: int = 0

    async def fetchrow(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetchrow_result

    async def fetch(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetch_result

    async def fetchval(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetchval_result

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "DELETE 1"

    def acquire(self):
        return _FakeAcquire(self)

    def transaction(self):
        return _FakeTransaction(self)


class _FakeAcquire:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool

    async def __aenter__(self):
        return self.pool

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeTransaction:
    def __init__(self, pool: _FakePool) -> None:
        self.pool = pool
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        self.pool.executed.append(("BEGIN", ()))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        self.pool.executed.append(("COMMIT" if exc_type is None else "ROLLBACK", ()))
        return False


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    repo = PgPresetRepository(pool_getter=lambda: pool)

    assert await repo.get("ghost", "indexation") is None


@pytest.mark.asyncio
async def test_get_returns_dict_when_found():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row()
    repo = PgPresetRepository(pool_getter=lambda: pool)

    result = await repo.get("default", "indexation")
    assert result is not None
    assert result["name"] == "default"
    assert result["config"] == {"chunk_size": 512}


@pytest.mark.asyncio
async def test_list_all_no_filter():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetch_result = [_make_row(), _make_row(name="legal")]
    repo = PgPresetRepository(pool_getter=lambda: pool)

    results = await repo.list_all()
    assert len(results) == 2
    query, params = pool.executed[0]
    assert "ORDER BY preset_type, name" in query
    assert params == ()


@pytest.mark.asyncio
async def test_list_all_with_type_filter():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetch_result = [_make_row()]
    repo = PgPresetRepository(pool_getter=lambda: pool)

    await repo.list_all(preset_type="retrieval")
    query, params = pool.executed[0]
    assert "WHERE preset_type" in query
    assert params == ("retrieval",)


@pytest.mark.asyncio
async def test_upsert_uses_on_conflict():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row(config={"chunk_size": 256})
    repo = PgPresetRepository(pool_getter=lambda: pool)

    result = await repo.upsert("default", "indexation", {"chunk_size": 256})
    assert result["config"] == {"chunk_size": 256}
    query, _ = pool.executed[0]
    assert "ON CONFLICT" in query
    assert "DO UPDATE" in query


@pytest.mark.asyncio
async def test_delete_returns_true_on_success():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    repo = PgPresetRepository(pool_getter=lambda: pool)

    assert await repo.delete("default", "indexation") is True


@pytest.mark.asyncio
async def test_delete_returns_false_when_missing():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()

    async def _execute(query, *params):
        pool.executed.append((query, params))
        return "DELETE 0"

    pool.execute = _execute
    repo = PgPresetRepository(pool_getter=lambda: pool)

    assert await repo.delete("ghost", "indexation") is False


@pytest.mark.asyncio
async def test_rename_migrates_partition_refs_in_one_transaction():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row(name="new", config={"chunk_size": 256})
    repo = PgPresetRepository(pool_getter=lambda: pool)

    result = await repo.rename("old", "new", "indexation", {"chunk_size": 256})

    assert result["name"] == "new"
    operations = [query for query, _params in pool.executed]
    # Atomic: insert the new row, repoint referencing partitions, drop the old row.
    assert operations[0] == "BEGIN"
    assert "INSERT INTO pipeline_presets" in operations[1]
    assert "UPDATE partitions" in operations[2]
    assert "indexation_preset" in operations[2]
    assert "DELETE FROM pipeline_presets" in operations[3]
    assert operations[-1] == "COMMIT"

    # The partition repoint runs old -> new on the indexation column.
    update_params = pool.executed[2][1]
    assert update_params == ("new", "old")


@pytest.mark.asyncio
async def test_rename_migrates_retrieval_column():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row(name="new", preset_type="retrieval", config={})
    repo = PgPresetRepository(pool_getter=lambda: pool)

    await repo.rename("old", "new", "retrieval", {})

    update_query = next(q for q, _ in pool.executed if "UPDATE partitions" in q)
    assert "retrieval_preset" in update_query


@pytest.mark.asyncio
async def test_count_partitions_using_indexation_queries_correct_column():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchval_result = 3
    repo = PgPresetRepository(pool_getter=lambda: pool)

    count = await repo.count_partitions_using("default", "indexation")
    assert count == 3
    query, params = pool.executed[0]
    assert "indexation_preset" in query
    assert params == ("default",)


@pytest.mark.asyncio
async def test_count_partitions_using_retrieval_queries_correct_column():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    pool._fetchval_result = 5
    repo = PgPresetRepository(pool_getter=lambda: pool)

    count = await repo.count_partitions_using("default", "retrieval")
    assert count == 5
    query, params = pool.executed[0]
    assert "retrieval_preset" in query
    assert params == ("default",)


@pytest.mark.asyncio
async def test_count_partitions_using_rejects_invalid_type():
    from services.persistence.preset_repo import PgPresetRepository

    pool = _FakePool()
    repo = PgPresetRepository(pool_getter=lambda: pool)

    with pytest.raises(ValueError, match="Invalid preset_type"):
        await repo.count_partitions_using("default", "bad-type")
    assert pool.executed == []
