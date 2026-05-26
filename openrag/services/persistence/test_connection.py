from unittest.mock import Mock

from services.persistence.connection import ConnectionManager


class RDBConfigStub:
    host = "db"
    port = 5432
    user = "root"
    password = "root_password"
    database = "partitions_for_collection_test"
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
