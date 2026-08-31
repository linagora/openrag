"""Unit tests for PgModelEndpointRepository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_row(**kwargs):
    base = {
        "name": "default",
        "model_type": "embedder",
        "endpoint": "http://vllm:8000/v1",
        "model_name": "jina-v3",
        "batch_size": 32,
        "timeout": 30.0,
        "extra": {},
        "is_default": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kwargs)
    return base


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        return False


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchrow_result = None
        self._fetch_result: list = []

    def transaction(self):
        return _AsyncCtx(self)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"

    async def fetch(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetch_result

    async def fetchrow(self, query: str, *params):
        self.executed.append((query, params))
        return self._fetchrow_result


class _FakePool:
    def __init__(self):
        self.conn = _FakeConn()
        self.executed: list[tuple[str, tuple]] = []
        self._fetchrow_result = None
        self._fetch_result: list = []
        self._fetchval_result = None

    def acquire(self):
        return _AsyncCtx(self.conn)

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


@pytest.mark.asyncio
async def test_create_inserts_and_returns_model():
    from core.config.model_endpoints import ModelEndpointRow
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = _make_row()
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    row = ModelEndpointRow(
        name="default",
        model_type="embedder",
        endpoint="http://vllm:8000/v1",
        is_default=False,
        created_at=_NOW,
        updated_at=_NOW,
    )
    result = await repo.create(row)

    assert result.name == "default"
    assert result.model_type == "embedder"
    queries = [q for q, _ in pool.conn.executed]
    assert any("INSERT INTO model_endpoints" in q for q in queries)
    # is_default=False on the row -> no demotion of an existing default.
    assert not any("is_default = false" in q for q in queries)


@pytest.mark.asyncio
async def test_create_default_demotes_existing_in_same_transaction():
    from core.config.model_endpoints import ModelEndpointRow
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = _make_row(is_default=True)
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    row = ModelEndpointRow(
        name="default",
        model_type="embedder",
        endpoint="http://vllm:8000/v1",
        is_default=True,
        created_at=_NOW,
        updated_at=_NOW,
    )
    await repo.create(row)

    queries = [q for q, _ in pool.conn.executed]
    # The clear UPDATE must precede the INSERT so the new row is the sole default.
    clear_idx = next(i for i, q in enumerate(queries) if "is_default = false" in q)
    insert_idx = next(i for i, q in enumerate(queries) if "INSERT INTO model_endpoints" in q)
    assert clear_idx < insert_idx


@pytest.mark.asyncio
async def test_create_maps_duplicate_to_typed_conflict():
    import asyncpg
    from core.config.model_endpoints import ModelEndpointRow
    from core.utils.exceptions import ValidationError
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()

    async def raise_duplicate(*_args, **_kwargs):
        raise asyncpg.UniqueViolationError("duplicate endpoint")

    pool.conn.fetchrow = raise_duplicate
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    row = ModelEndpointRow(
        name="default",
        model_type="embedder",
        endpoint="http://vllm:8000/v1",
        is_default=False,
        created_at=_NOW,
        updated_at=_NOW,
    )

    with pytest.raises(ValidationError) as exc_info:
        await repo.create(row)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "ENDPOINT_EXISTS"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool._fetchrow_result = None
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    assert await repo.get("missing", "embedder") is None


@pytest.mark.asyncio
async def test_list_all_no_filter_orders_by_type_name():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool._fetch_result = [_make_row(), _make_row(name="fast", model_type="llm")]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    results = await repo.list_all()
    assert len(results) == 2
    query, params = pool.executed[0]
    assert "ORDER BY model_type, name" in query
    assert params == ()


@pytest.mark.asyncio
async def test_list_all_filters_by_model_type():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool._fetch_result = [_make_row()]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.list_all(model_type="embedder")
    query, params = pool.executed[0]
    assert "WHERE model_type" in query
    assert params == ("embedder",)


@pytest.mark.asyncio
async def test_update_builds_set_clause_for_allowed_fields():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row(endpoint="http://new:8000/v1")
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    result = await repo.update("default", "embedder", endpoint="http://new:8000/v1")
    assert result is not None
    query, params = pool.executed[0]
    assert "UPDATE model_endpoints SET" in query
    assert "endpoint = $3" in query
    assert "updated_at = now()" in query
    assert params[2] == "http://new:8000/v1"


@pytest.mark.asyncio
async def test_update_ignores_unknown_fields():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool._fetchrow_result = _make_row()
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    # "unknown_field" should be silently ignored; falls back to a plain GET
    await repo.update("default", "embedder", unknown_field="x")
    query, _ = pool.executed[0]
    assert "SELECT" in query


@pytest.mark.asyncio
async def test_delete_returns_true_on_success():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    assert await repo.delete("default", "embedder") is True


@pytest.mark.asyncio
async def test_delete_returns_false_when_row_missing():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()

    async def _execute(query, *params):
        pool.executed.append((query, params))
        return "DELETE 0"

    pool.execute = _execute
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    assert await repo.delete("ghost", "embedder") is False


@pytest.mark.asyncio
async def test_rename_updates_the_model_endpoints_row():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old", "embedder", "new")

    queries = [q for q, _ in pool.conn.executed]
    assert any("UPDATE model_endpoints SET name = $3" in q for q in queries)
    params = next(p for q, p in pool.conn.executed if "UPDATE model_endpoints SET name" in q)
    assert params == ("old", "embedder", "new")


@pytest.mark.asyncio
async def test_rename_locks_partitions_table_before_touching_model_endpoints():
    """rename() must LOCK partitions IN SHARE MODE before its own UPDATE —
    the same order PgPartitionRepository.update_partition's chat_llm guard
    touches partitions (write) then model_endpoints (check), so the two
    transactions can only block on each other, never deadlock. Without this
    lock, a partition PATCH could validate 'old' in-memory, block on this
    transaction's cascade instead, then resume and write 'old' straight back
    after this commits — see PgPartitionRepository.update_partition."""
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old", "llm", "new")

    queries = [q for q, _ in pool.conn.executed]
    lock_i = next(i for i, q in enumerate(queries) if q == "LOCK TABLE partitions IN SHARE MODE")
    rename_i = next(i for i, q in enumerate(queries) if "UPDATE model_endpoints SET name" in q)
    assert lock_i < rename_i


@pytest.mark.asyncio
async def test_rename_raises_not_found_and_skips_cascade_when_row_vanished():
    """A concurrent delete between the service's existence check and this
    transaction must abort before the cascade — not repoint partitions/presets
    at a `new_name` that was never actually created (mirrors
    PgPipelinePresetRepository.rename's RETURNING guard)."""
    from core.utils.exceptions import NotFoundError
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()  # _fetchrow_result defaults to None: row is gone
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    with pytest.raises(NotFoundError):
        await repo.rename("old-llm", "llm", "new-llm")

    queries = [q for q, _ in pool.conn.executed]
    assert not any("UPDATE partitions SET" in q for q in queries)
    assert not any("pipeline_presets" in q for q in queries)


@pytest.mark.asyncio
async def test_rename_embedder_cascades_to_partitions_embedder_only():
    """Renaming an embedder must update `partitions.embedder` and touch no
    preset JSONB — the embedder name isn't referenced inside any preset."""
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new-embedder"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old-embedder", "embedder", "new-embedder")

    queries = pool.conn.executed
    partition_updates = [(q, p) for q, p in queries if "UPDATE partitions SET" in q]
    assert len(partition_updates) == 1
    q, p = partition_updates[0]
    assert "embedder = $2 WHERE embedder = $1" in q
    assert p == ("old-embedder", "new-embedder")
    assert not any("pipeline_presets" in q for q, _ in queries)


@pytest.mark.asyncio
async def test_rename_llm_cascades_to_chat_llm_and_both_preset_types():
    """Renaming an LLM endpoint must update `partitions.chat_llm`, the
    retrieval preset's `llm` key, and every indexation-preset LLM field."""
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new-llm"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old-llm", "llm", "new-llm")

    queries = pool.conn.executed
    partition_updates = [(q, p) for q, p in queries if "UPDATE partitions SET" in q]
    assert len(partition_updates) == 1
    q, p = partition_updates[0]
    assert "chat_llm = $2 WHERE chat_llm = $1" in q
    assert p == ("old-llm", "new-llm")

    preset_updates = [(q, p) for q, p in queries if "pipeline_presets" in q]
    # retrieval.llm + indexation.{contextualization_llm, metadata_extraction_llm, topic_tagging_llm}
    assert len(preset_updates) == 4
    keys_by_preset_type = {(p[2], p[3]) for _, p in preset_updates}
    assert keys_by_preset_type == {
        ("retrieval", "llm"),
        ("indexation", "contextualization_llm"),
        ("indexation", "metadata_extraction_llm"),
        ("indexation", "topic_tagging_llm"),
    }
    for _, p in preset_updates:
        assert p[0] == [p[3]]  # jsonb_set path matches the ->> key checked in WHERE
        assert p[1] == "new-llm"
        assert p[4] == "old-llm"


@pytest.mark.asyncio
async def test_rename_reranker_cascades_to_retrieval_preset_only():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new-ranker"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old-ranker", "reranker", "new-ranker")

    queries = pool.conn.executed
    assert not any("UPDATE partitions SET" in q for q, _ in queries)
    preset_updates = [(q, p) for q, p in queries if "pipeline_presets" in q]
    assert len(preset_updates) == 1
    q, p = preset_updates[0]
    assert p == (["reranker"], "new-ranker", "retrieval", "reranker", "old-ranker")


@pytest.mark.asyncio
async def test_rename_vlm_cascades_to_indexation_preset_only():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new-vlm"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old-vlm", "vlm", "new-vlm")

    queries = pool.conn.executed
    assert not any("UPDATE partitions SET" in q for q, _ in queries)
    preset_updates = [(q, p) for q, p in queries if "pipeline_presets" in q]
    assert len(preset_updates) == 1
    q, p = preset_updates[0]
    assert p == (["vlm"], "new-vlm", "indexation", "vlm", "old-vlm")


@pytest.mark.asyncio
async def test_rename_stt_cascades_to_indexation_preset_only():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetchrow_result = {"name": "new-moss"}
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.rename("old-moss", "stt", "new-moss")

    queries = pool.conn.executed
    assert not any("UPDATE partitions SET" in q for q, _ in queries)
    preset_updates = [(q, p) for q, p in queries if "pipeline_presets" in q]
    assert len(preset_updates) == 1
    _, params = preset_updates[0]
    assert params == (["stt"], "new-moss", "indexation", "stt", "old-moss")


def _row(name, is_default):
    return {"name": name, "is_default": is_default}


@pytest.mark.asyncio
async def test_set_default_locks_rows_then_runs_two_updates():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetch_result = [_row("default", True), _row("jina", False)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.set_default("embedder", "jina")

    queries = [q for q, _ in pool.conn.executed]
    # Decide under a row lock, then clear-then-set inside the same transaction.
    assert any("FOR UPDATE" in q for q in queries)
    assert any("is_default = false" in q for q in queries)
    assert any("is_default = true" in q for q in queries)


@pytest.mark.asyncio
async def test_set_default_raises_not_found_without_clearing_when_target_missing():
    from core.utils.exceptions import NotFoundError
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    # 'ghost' is absent from the locked rows (e.g. deleted concurrently). set_default
    # must abort BEFORE clearing the existing default, so the type is never left
    # without one.
    pool = _FakePool()
    pool.conn._fetch_result = [_row("jina", True)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    with pytest.raises(NotFoundError):
        await repo.set_default("embedder", "ghost")

    queries = [q for q, _ in pool.conn.executed]
    assert any("FOR UPDATE" in q for q in queries)
    assert not any("is_default = false" in q for q in queries)
    assert not any("is_default = true" in q for q in queries)


@pytest.mark.asyncio
async def test_delete_and_promote_not_found_no_delete():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetch_result = [_row("e5", False), _row("jina", True)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    status, promoted = await repo.delete_and_promote_default("ghost", "embedder")
    assert status == "not_found"
    assert promoted is None
    assert not any("DELETE FROM model_endpoints" in q for q, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_delete_and_promote_last_no_delete():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetch_result = [_row("jina", True)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    status, promoted = await repo.delete_and_promote_default("jina", "embedder")
    assert status == "last"
    assert promoted is None
    assert not any("DELETE FROM model_endpoints" in q for q, _ in pool.conn.executed)


@pytest.mark.asyncio
async def test_delete_and_promote_non_default_deletes_no_promotion():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetch_result = [_row("e5", False), _row("jina", True)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    status, promoted = await repo.delete_and_promote_default("e5", "embedder")
    assert status == "ok"
    assert promoted is None
    queries = [q for q, _ in pool.conn.executed]
    assert any("FOR UPDATE" in q for q in queries)
    assert any("DELETE FROM model_endpoints" in q for q in queries)
    assert not any("is_default = false" in q for q in queries)
    assert not any("is_default = true" in q for q in queries)


@pytest.mark.asyncio
async def test_delete_and_promote_default_promotes_survivor_under_lock():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    pool.conn._fetch_result = [_row("e5", False), _row("jina", True)]
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    status, promoted = await repo.delete_and_promote_default("jina", "embedder")
    assert status == "ok"
    assert promoted == "e5"  # first survivor by name
    queries = [q for q, _ in pool.conn.executed]
    assert any("FOR UPDATE" in q for q in queries)
    assert any("DELETE FROM model_endpoints" in q for q in queries)
    assert any("is_default = false" in q for q in queries)
    assert any("is_default = true" in q for q in queries)
