"""Tests for Settings.resolved_rdb().

``rdb.database`` is optional in config; every process that opens its own
Postgres connection has to derive the same name. A Ray worker that skipped
this derivation died in its constructor with "RDBConfig.database is required",
which surfaced only as a run stuck in QUEUED — hence the coverage.
"""

from __future__ import annotations

from core.config.infrastructure import RDBConfig, VectorDBConfig
from core.config.root import Settings


def _settings(**rdb_fields) -> Settings:
    return Settings(
        rdb=RDBConfig(**rdb_fields),
        vectordb=VectorDBConfig(collection_name="my_collection"),
    )


def test_derives_the_database_name_from_the_collection_when_unset():
    assert _settings(database=None).resolved_rdb().database == "partitions_for_collection_my_collection"


def test_keeps_an_explicit_database_name():
    assert _settings(database="explicit_db").resolved_rdb().database == "explicit_db"


def test_does_not_mutate_the_original_config():
    settings = _settings(database=None)

    settings.resolved_rdb()

    assert settings.rdb.database is None


def test_preserves_the_other_connection_fields():
    resolved = _settings(database=None, host="db.internal", port=6543).resolved_rdb()

    assert resolved.host == "db.internal"
    assert resolved.port == 6543
