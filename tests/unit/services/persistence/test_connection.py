from unittest.mock import AsyncMock, Mock

import pytest
from services.persistence.connection import ConnectionManager


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


def test_ensure_database_exists_creates_missing_database(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())
    create_database = Mock()

    monkeypatch.setattr("sqlalchemy_utils.database_exists", lambda url: False)
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    manager._ensure_database_exists()

    create_database.assert_called_once()


def test_ensure_database_exists_skips_existing_database(monkeypatch):
    manager = ConnectionManager(RDBConfigStub())
    create_database = Mock()

    monkeypatch.setattr("sqlalchemy_utils.database_exists", lambda url: True)
    monkeypatch.setattr("sqlalchemy_utils.create_database", create_database)

    manager._ensure_database_exists()

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
