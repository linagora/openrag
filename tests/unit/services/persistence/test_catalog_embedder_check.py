"""The catalog write refuses a file its partition's embedder no longer matches (#958).

A file's vectors are stored before its catalog row, built from the endpoint
config the indexer held at the time. These pin down the SQL order that makes
the check race-free; tests/integration/repos/test_embedder_pinning.py runs it
against two real Postgres connections.
"""

from __future__ import annotations

import pytest
from core.config.model_endpoints import embedder_fingerprint

_JINA = {"name": "jina", "endpoint": "http://jina:8000/v1", "model_name": "jina-v3", "extra": {}}
_BUILT_WITH_JINA = embedder_fingerprint("http://jina:8000/v1", "jina-v3", {})


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, *, partition_embedder: str | None = "jina", endpoint: dict | None = None):
        self.executed: list[tuple[str, tuple]] = []
        self.partition_embedder = partition_embedder
        self.endpoint = endpoint
        self.in_transaction = False

    def transaction(self):
        self.in_transaction = True
        return _AsyncContext(self)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"

    async def fetchval(self, query: str, *params):
        self.executed.append((query, params))
        if "SELECT embedder FROM partitions" in query:
            return self.partition_embedder
        if "SELECT 1 FROM partitions" in query:
            return 1
        return None

    async def fetchrow(self, query: str, *params):
        self.executed.append((query, params))
        if "FROM model_endpoints" in query:
            return self.endpoint
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn
        self.executed: list[tuple[str, tuple]] = []

    def acquire(self):
        return _AsyncContext(self.conn)

    async def execute(self, query: str, *params):
        self.executed.append((query, params))
        return "UPDATE 1"


def _repo(conn: _FakeConn):
    from services.persistence.document_repo import PgDocumentRepository

    pool = _FakePool(conn)
    return PgDocumentRepository(pool_getter=lambda: pool), pool


def _position(conn: _FakeConn, fragment: str) -> int:
    return next(i for i, (query, _) in enumerate(conn.executed) if fragment in query)


@pytest.mark.asyncio
async def test_records_a_file_built_by_the_partitions_current_embedder():
    conn = _FakeConn(endpoint=_JINA)
    repo, _ = _repo(conn)

    added = await repo.add_file_to_partition(
        file_id="f1", partition="docs", user_id=1, embedder_fingerprint=_BUILT_WITH_JINA
    )

    assert added is True
    # `partitions` first, the order set_default takes, then the endpoint row,
    # all before the row that makes the file visible.
    assert (
        _position(conn, "LOCK TABLE partitions IN ROW EXCLUSIVE MODE")
        < _position(conn, "SELECT embedder FROM partitions")
        < _position(conn, "FROM model_endpoints")
        < _position(conn, "INSERT INTO files")
    )
    assert "FOR SHARE" in conn.executed[_position(conn, "SELECT embedder FROM partitions")][0]
    assert "FOR SHARE" in conn.executed[_position(conn, "FROM model_endpoints")][0]


@pytest.mark.asyncio
async def test_refuses_a_file_built_before_its_embedder_was_edited():
    from core.utils.exceptions import ConflictError

    conn = _FakeConn(endpoint={**_JINA, "model_name": "jina-v4"})
    repo, _ = _repo(conn)

    with pytest.raises(ConflictError) as exc:
        await repo.add_file_to_partition(
            file_id="f1", partition="docs", user_id=1, embedder_fingerprint=_BUILT_WITH_JINA
        )

    assert exc.value.code == "EMBEDDER_CHANGED_DURING_INDEXING"
    assert "Embedder 'jina' of partition 'docs' changed (model_name)" in exc.value.message
    assert not any("INSERT INTO files" in query for query, _ in conn.executed)


@pytest.mark.asyncio
async def test_resolves_a_partition_on_the_alias_to_the_default_endpoint():
    conn = _FakeConn(partition_embedder="default", endpoint=_JINA)
    repo, _ = _repo(conn)

    await repo.add_file_to_partition(file_id="f1", partition="docs", user_id=1, embedder_fingerprint=_BUILT_WITH_JINA)

    sql, params = conn.executed[_position(conn, "FROM model_endpoints")]
    assert "is_default" in sql
    assert params == ("default", "default")


@pytest.mark.asyncio
async def test_records_the_file_when_no_endpoint_is_registered_to_compare_with():
    """An env-only embedder has no row an admin could have edited."""
    conn = _FakeConn(endpoint=None)
    repo, _ = _repo(conn)

    assert await repo.add_file_to_partition(
        file_id="f1", partition="docs", user_id=1, embedder_fingerprint=_BUILT_WITH_JINA
    )


@pytest.mark.asyncio
async def test_without_a_fingerprint_nothing_is_locked():
    conn = _FakeConn(endpoint=_JINA)
    repo, _ = _repo(conn)

    await repo.add_file_to_partition(file_id="f1", partition="docs", user_id=1)

    assert not any("LOCK TABLE" in query or "model_endpoints" in query for query, _ in conn.executed)


@pytest.mark.asyncio
async def test_a_reindex_checks_in_the_transaction_that_updates_the_row():
    from core.utils.exceptions import ConflictError

    conn = _FakeConn(endpoint={**_JINA, "endpoint": "http://elsewhere:8000/v1"})
    repo, pool = _repo(conn)

    with pytest.raises(ConflictError):
        await repo.update_file_in_partition("f1", "docs", indexation_config={}, embedder_fingerprint=_BUILT_WITH_JINA)

    assert conn.in_transaction
    assert not any("UPDATE files" in query for query, _ in conn.executed)
    assert pool.executed == []


@pytest.mark.asyncio
async def test_a_reindex_built_by_the_current_embedder_updates_the_row():
    conn = _FakeConn(endpoint=_JINA)
    repo, _ = _repo(conn)

    assert await repo.update_file_in_partition(
        "f1", "docs", indexation_config={}, embedder_fingerprint=_BUILT_WITH_JINA
    )
    assert _position(conn, "FROM model_endpoints") < _position(conn, "UPDATE files")
