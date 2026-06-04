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

    def transaction(self):
        return _AsyncCtx(self)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"

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
    pool._fetchrow_result = _make_row()
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    row = ModelEndpointRow(
        name="default",
        model_type="embedder",
        endpoint="http://vllm:8000/v1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    result = await repo.create(row)

    assert result.name == "default"
    assert result.model_type == "embedder"
    assert any("INSERT INTO model_endpoints" in q for q, _ in pool.executed)


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
async def test_set_default_uses_transaction_with_two_updates():
    from services.persistence.model_endpoint_repo import PgModelEndpointRepository

    pool = _FakePool()
    repo = PgModelEndpointRepository(pool_getter=lambda: pool)

    await repo.set_default("embedder", "default")

    queries = [q for q, _ in pool.conn.executed]
    assert any("is_default = false" in q for q in queries)
    assert any("is_default = true" in q for q in queries)
