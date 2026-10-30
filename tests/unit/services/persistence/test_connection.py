from unittest.mock import AsyncMock, Mock

import asyncpg
import pytest
from services.persistence.connection import ConnectionManager
from sqlalchemy.exc import OperationalError


class RDBConfigStub:
    host = "db"
    port = 5432
    user = "root"
    password = "root_password"
    database = "partitions_for_collection_test"
    auto_create_database = True
    pool_min_size = 1
    pool_max_size = 4
    command_timeout = 10


@pytest.mark.asyncio
async def test_ensure_database_exists_creates_missing_database(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())
    create_database = Mock()

    monkeypatch.setattr(manager, "_database_exists", AsyncMock(return_value=False))
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    await manager._ensure_database_exists()

    create_database.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_database_exists_skips_existing_database(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())
    create_database = Mock()

    monkeypatch.setattr(manager, "_database_exists", AsyncMock(return_value=True))
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    await manager._ensure_database_exists()

    create_database.assert_not_called()


@pytest.mark.asyncio
async def test_initialize_skips_database_creation_when_auto_create_is_disabled(monkeypatch):
    class NoCreateDBConfig(RDBConfigStub):
        auto_create_database = False

    manager = ConnectionManager(NoCreateDBConfig())
    ensure_database_exists = Mock()
    create_pool = AsyncMock(return_value=object())

    monkeypatch.setattr(manager, "_ensure_database_exists", ensure_database_exists)
    monkeypatch.setattr("services.persistence.connection.asyncpg.create_pool", create_pool)

    await manager.initialize()

    ensure_database_exists.assert_not_called()
    create_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_database_exists_tolerates_concurrent_creation(monkeypatch):
    # Another process creates the database between our check and our create (#1045).
    manager = ConnectionManager(RDBConfigStub())
    database_exists = AsyncMock(side_effect=[False, True])
    create_database = Mock(side_effect=RuntimeError("already exists"))

    monkeypatch.setattr(manager, "_database_exists", database_exists)
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    await manager._ensure_database_exists()

    create_database.assert_called_once()
    assert database_exists.await_count == 2


@pytest.mark.asyncio
async def test_ensure_database_exists_reraises_when_database_still_missing(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())

    monkeypatch.setattr(manager, "_database_exists", AsyncMock(return_value=False))
    monkeypatch.setattr("sqlalchemy_utils.create_database", Mock(side_effect=RuntimeError("permission denied")))

    with pytest.raises(RuntimeError, match="permission denied"):
        await manager._ensure_database_exists()


@pytest.mark.asyncio
async def test_initialize_retries_when_postgres_drops_during_database_creation(monkeypatch):
    # The server is up for the check, drops during the create, and is back for
    # the recheck, so the database is still missing.
    manager = ConnectionManager(RDBConfigStub())
    create_pool = AsyncMock(return_value=object())
    create_database = Mock(
        side_effect=[OperationalError("CREATE DATABASE", {}, Exception("server closed the connection")), None]
    )

    monkeypatch.setattr(manager, "_database_exists", AsyncMock(side_effect=[False, False, False]))
    monkeypatch.setattr("services.persistence.connection.asyncpg.create_pool", create_pool)
    monkeypatch.setattr("services.persistence.connection.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    await manager.initialize()

    assert create_database.call_count == 2
    create_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialize_retries_database_check_until_postgres_is_up(monkeypatch):
    # Postgres refuses connections for the first attempts (#1159).
    manager = ConnectionManager(RDBConfigStub())
    conn = AsyncMock()
    connect = AsyncMock(side_effect=[ConnectionRefusedError(), ConnectionRefusedError(), conn])
    create_pool = AsyncMock(return_value=object())
    create_database = Mock(side_effect=RuntimeError("connection refused"))

    monkeypatch.setattr("services.persistence.connection.asyncpg.connect", connect)
    monkeypatch.setattr("services.persistence.connection.asyncpg.create_pool", create_pool)
    monkeypatch.setattr("services.persistence.connection.asyncio.sleep", AsyncMock())
    # sqlalchemy_utils reports a refused connection as a missing database.
    monkeypatch.setattr("sqlalchemy_utils.database_exists", lambda url: False)
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    await manager.initialize()

    create_database.assert_not_called()
    assert connect.await_count == 3
    conn.close.assert_awaited_once()
    create_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_database_exists_is_false_only_for_missing_database(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())

    monkeypatch.setattr(
        "services.persistence.connection.asyncpg.connect",
        AsyncMock(side_effect=asyncpg.InvalidCatalogNameError("database does not exist")),
    )
    assert await manager._database_exists() is False

    monkeypatch.setattr(
        "services.persistence.connection.asyncpg.connect",
        AsyncMock(side_effect=ConnectionRefusedError()),
    )
    with pytest.raises(ConnectionRefusedError):
        await manager._database_exists()
