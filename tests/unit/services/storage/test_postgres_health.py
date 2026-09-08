from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from services.storage.postgres_store import PostgresStore


async def test_health_uses_the_live_pool_and_propagates_query_failure():
    connection = SimpleNamespace(fetchval=AsyncMock(return_value=1))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    store = object.__new__(PostgresStore)
    store._conn = SimpleNamespace(pool=pool)

    await store.check_health()
    pool.acquire.assert_called_once_with(timeout=2.0)
    connection.fetchval.assert_awaited_once_with("SELECT 1", timeout=2.0)

    connection.fetchval.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await store.check_health()
