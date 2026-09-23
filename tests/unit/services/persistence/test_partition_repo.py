from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime

import pytest
from core.utils.exceptions import ValidationError


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, *, owned_count: int = 0, existing_partition: bool = False):
        self.owned_count = owned_count
        self.existing_partition = existing_partition
        self.operations: list[tuple[str, tuple]] = []
        self.inserted_partition = False

    def transaction(self):
        self.operations.append(("BEGIN", ()))
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT * FROM partitions" in query:
            if self.existing_partition:
                return {"partition": params[0], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
            return None
        if "INSERT INTO partitions" in query:
            self.inserted_partition = True
            return {"partition": params[0], "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
        return None

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "SELECT EXISTS (SELECT 1 FROM partitions" in query:
            return self.existing_partition
        if "COUNT(*)::int FROM partition_memberships" in query:
            return self.owned_count
        return None

    async def execute(self, query: str, *params):
        self.operations.append((query, params))
        return "INSERT 0 1"


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn
        self.acquire_count = 0

    def acquire(self):
        self.acquire_count += 1
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_create_partition_enforces_owner_cap_inside_transaction():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(owned_count=2)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    with pytest.raises(ValidationError) as exc:
        await repo.create_partition("new", user_id=7, max_owned=2)

    assert exc.value.code == "PARTITION_LIMIT_EXCEEDED"
    assert conn.inserted_partition is False
    operations = [query for query, _params in conn.operations]
    assert any("pg_advisory_xact_lock" in query for query in operations)
    assert any("COUNT(*)::int FROM partition_memberships" in query for query in operations)
    assert not any("INSERT INTO partitions" in query for query in operations)


@pytest.mark.asyncio
async def test_create_partition_locks_user_before_counting_owned_partitions():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(owned_count=1)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    await repo.create_partition("new", user_id=7, max_owned=2)

    operations = [query for query, _params in conn.operations]
    lock_index = next(i for i, query in enumerate(operations) if "pg_advisory_xact_lock" in query)
    count_index = next(i for i, query in enumerate(operations) if "COUNT(*)::int FROM partition_memberships" in query)
    insert_index = next(i for i, query in enumerate(operations) if "INSERT INTO partitions" in query)
    assert lock_index < count_index < insert_index


@pytest.mark.asyncio
async def test_create_partition_existing_row_raises_conflict():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(existing_partition=True)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    with pytest.raises(ValidationError) as exc:
        await repo.create_partition("existing", user_id=7, max_owned=2)

    assert exc.value.status_code == 409
    assert exc.value.code == "PARTITION_EXISTS"
    assert conn.inserted_partition is False


@pytest.mark.asyncio
async def test_partition_operation_lock_uses_session_advisory_lock_and_unlocks_on_error():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn()
    pool = _FakePool(conn)
    repo = PgPartitionRepository(pool_getter=lambda: pool)

    with pytest.raises(RuntimeError, match="inside fence"):
        async with repo.partition_operation_lock("tenant-a"):
            raise RuntimeError("inside fence")

    operations = [(query, params) for query, params in conn.operations if "pg_advisory_" in query]
    assert [query for query, _ in operations] == [
        "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
        "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
    ]
    assert operations[0][1] == operations[1][1]
    assert operations[0][1][1] == "tenant-a"
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_partition_operation_guard_checks_existence_on_lock_connection():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _FakeConn(existing_partition=True)
    pool = _FakePool(conn)
    repo = PgPartitionRepository(pool_getter=lambda: pool)

    async with repo.partition_operation_lock("tenant-a") as operation:
        assert await operation.partition_exists("tenant-a") is True

    operations = [query for query, _params in conn.operations]
    assert operations == [
        "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
        "SELECT EXISTS (SELECT 1 FROM partitions WHERE partition = $1)",
        "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
    ]
    assert pool.acquire_count == 1


# ── update_partition preset-assignment race guard ────────────────────


def _full_partition_row(**overrides):
    row = {
        "partition": "p1",
        "description": None,
        "embedder": "default",
        "indexation_preset": "legal",
        "retrieval_preset": "default",
        "dimension": None,
        "collection_name": None,
        "chat_history_depth": 0,
        "chat_llm": None,
        "generation_prompt_names": {},
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


class _UpdateFakeConn:
    """Conn/pool double for exercising update_partition's transactional guard.

    ``preset_exists`` models whether the referenced preset row is still present
    when the guard's follow-up SELECT runs (False simulates a concurrent
    delete_preset committing while this UPDATE was blocked on its SHARE lock).
    ``model_endpoint_exists`` is the same, for a ``chat_llm`` reference racing
    a concurrent ``PgModelEndpointRepository.rename``.
    """

    def __init__(self, *, preset_exists: bool = True, model_endpoint_exists: bool = True):
        self.preset_exists = preset_exists
        self.model_endpoint_exists = model_endpoint_exists
        self.operations: list[tuple[str, tuple]] = []
        self.transactions = 0

    # pool interface
    def acquire(self):
        return _AsyncContext(self)

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "UPDATE partitions" in query:
            # Reflect the assigned columns back so the returned row matches the
            # write. ``$n`` placeholders are 1-indexed into params ($1 = name).
            body = query.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            update = {}
            for frag in body.split(", "):
                col, _, ref = frag.strip().partition(" = $")
                if ref.isdigit():
                    update[col.strip()] = params[int(ref) - 1]
            return _full_partition_row(**update)
        return None

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "FROM pipeline_presets" in query:
            return 1 if self.preset_exists else None
        if "FROM model_endpoints" in query:
            return 1 if self.model_endpoint_exists else None
        return None

    # conn interface
    def transaction(self):
        self.transactions += 1
        self.operations.append(("BEGIN", ()))
        return _AsyncContext(self)


@pytest.mark.asyncio
async def test_update_partition_rolls_back_when_preset_deleted_concurrently():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    with pytest.raises(ValidationError) as exc:
        await repo.update_partition("p1", indexation_preset="just-deleted")

    assert exc.value.code == "PRESET_NOT_FOUND"
    # The write and the existence check share one transaction, and the write
    # (partitions) happens before the check (pipeline_presets) — the lock order
    # that keeps it deadlock-free against delete_preset. Raising inside the
    # transaction rolls the UPDATE back.
    assert conn.transactions == 1
    queries = [q for q, _ in conn.operations]
    update_i = next(i for i, q in enumerate(queries) if "UPDATE partitions" in q)
    check_i = next(i for i, q in enumerate(queries) if "FROM pipeline_presets" in q)
    assert update_i < check_i


@pytest.mark.asyncio
async def test_update_partition_commits_when_preset_exists():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=True)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", indexation_preset="legal")

    assert result["indexation_preset"] == "legal"
    assert conn.transactions == 1
    assert any("FROM pipeline_presets" in q for q, _ in conn.operations)


@pytest.mark.asyncio
async def test_update_partition_without_preset_change_skips_the_guard():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(preset_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", description="notes")

    # Non-preset updates take the fast path: no transaction, no preset lookup.
    assert result["description"] == "notes"
    assert conn.transactions == 0
    assert not any("FROM pipeline_presets" in q for q, _ in conn.operations)


# ── update_partition chat_llm-assignment race guard ──────────────────


@pytest.mark.asyncio
async def test_update_partition_rolls_back_when_chat_llm_endpoint_renamed_concurrently():
    """A concurrent PgModelEndpointRepository.rename() moving 'old-name' away
    while this UPDATE was blocked on the LOCK it also takes must not let the
    partition silently keep pointing at the now-nonexistent name."""
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    with pytest.raises(ValidationError) as exc:
        await repo.update_partition("p1", chat_llm="old-name")

    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    # The write and the existence check share one transaction, and the write
    # (partitions) happens before the check (model_endpoints) — the same lock
    # order PgModelEndpointRepository.rename uses, keeping the two deadlock-free.
    assert conn.transactions == 1
    queries = [q for q, _ in conn.operations]
    update_i = next(i for i, q in enumerate(queries) if "UPDATE partitions" in q)
    check_i = next(i for i, q in enumerate(queries) if "FROM model_endpoints" in q)
    assert update_i < check_i


@pytest.mark.asyncio
async def test_update_partition_commits_when_chat_llm_endpoint_exists():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=True)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", chat_llm="gpt-4.1")

    assert result["chat_llm"] == "gpt-4.1"
    assert conn.transactions == 1
    assert any("FROM model_endpoints" in q for q, _ in conn.operations)


@pytest.mark.asyncio
async def test_update_partition_clearing_chat_llm_skips_the_guard():
    """chat_llm=None clears the (nullable) column — it names no endpoint to
    validate, so this must take the fast, non-transactional path."""
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", chat_llm=None)

    assert result["chat_llm"] is None
    assert conn.transactions == 0
    assert not any("FROM model_endpoints" in q for q, _ in conn.operations)


@pytest.mark.asyncio
async def test_update_partition_rolls_back_when_embedder_endpoint_vanished():
    """An embedder assignment takes the same transactional guard as chat_llm —
    and needs it more, since a partition pointing at a nonexistent embedder
    fails every upload and every query rather than falling back."""
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    with pytest.raises(ValidationError) as exc:
        await repo.update_partition("p1", embedder="some-embedder")

    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert conn.transactions == 1
    queries = [q for q, _ in conn.operations]
    update_i = next(i for i, q in enumerate(queries) if "UPDATE partitions" in q)
    check_i = next(i for i, q in enumerate(queries) if "FROM model_endpoints" in q)
    assert update_i < check_i


@pytest.mark.asyncio
async def test_update_partition_commits_when_embedder_endpoint_exists():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=True)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    result = await repo.update_partition("p1", embedder="some-embedder")

    assert result["embedder"] == "some-embedder"
    assert conn.transactions == 1
    checks = [(q, params) for q, params in conn.operations if "FROM model_endpoints" in q]
    assert checks and checks[0][1] == ("some-embedder", "embedder")


@pytest.mark.asyncio
async def test_update_partition_embedder_check_resolves_the_default_alias():
    """`default` is a virtual name — load_all files the is_default row under it,
    so no model_endpoints row is called that. The guard has to match on the flag
    or every partition create (which assigns embedder='default') would 422."""
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _UpdateFakeConn(model_endpoint_exists=True)
    repo = PgPartitionRepository(pool_getter=lambda: conn)

    await repo.update_partition("p1", embedder="default")

    check = next(q for q, _ in conn.operations if "FROM model_endpoints" in q)
    assert "is_default" in check


class _SlowLockConn:
    """A copy-lock session that grants the lock, then holds back its reply until ``reply`` is set."""

    def __init__(self) -> None:
        self.held: Counter[str] = Counter()
        self.granted = asyncio.Event()
        self.reply = asyncio.Event()

    def is_closed(self) -> bool:
        return False

    def add_termination_listener(self, callback) -> None:
        pass

    async def execute(self, query: str, _namespace: int, name: str) -> None:
        if "unlock" in query:
            self.held[name] -= 1
            return
        self.held[name] += 1
        self.granted.set()
        await self.reply.wait()


def _connect_to(conn):
    async def connect():
        return conn

    return connect


@pytest.mark.asyncio
async def test_a_copy_lock_granted_to_a_cancelled_copy_is_released():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _SlowLockConn()

    async def connect():
        return conn

    repo = PgPartitionRepository(pool_getter=lambda: None, connect=connect)

    async def copy() -> None:
        async with repo.copy_lock("p1"):
            pytest.fail("a copy cancelled while taking its lock must not run")

    running = asyncio.create_task(copy())
    await conn.granted.wait()
    running.cancel()
    conn.reply.set()

    with pytest.raises(asyncio.CancelledError):
        await running
    # Left held, the lock would block the partition's embedder changes until the process exits.
    assert conn.held["p1"] == 0


# ---------------------------------------------------------------------------
# Embedder swaps
# ---------------------------------------------------------------------------


class _SwapConn(_FakeConn):
    def __init__(self, *, endpoint_exists: bool = True, inserted: dict | None = None, lock_acquired: bool = True):
        super().__init__()
        self.endpoint_exists = endpoint_exists
        self.inserted = inserted
        self.lock_acquired = lock_acquired
        self.terminated = lambda _conn: None

    def is_closed(self) -> bool:
        return False

    def add_termination_listener(self, callback) -> None:
        self.terminated = callback

    def terminate(self) -> None:
        pass

    async def fetchval(self, query: str, *params):
        self.operations.append((query, params))
        if "FROM model_endpoints" in query:
            return 1 if self.endpoint_exists else None
        if "pg_try_advisory_lock" in query:
            return self.lock_acquired
        return None

    async def fetchrow(self, query: str, *params):
        self.operations.append((query, params))
        if "INSERT INTO partition_embedder_swaps" in query:
            return self.inserted
        return None


@pytest.mark.asyncio
async def test_a_swap_is_recorded_after_locking_out_endpoint_deletes_and_checking_its_target():
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _SwapConn(inserted={"partition": "p1", "status": "running"})
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    swap = await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=4)

    assert swap == {"partition": "p1", "status": "running"}
    queries = [query for query, _ in conn.operations]
    lock = queries.index("LOCK TABLE partitions IN ROW EXCLUSIVE MODE")
    check = next(i for i, q in enumerate(queries) if "FROM model_endpoints" in q)
    insert = next(i for i, q in enumerate(queries) if "INSERT INTO partition_embedder_swaps" in q)
    assert queries[0] == "BEGIN"
    assert lock < check < insert
    # An edit changing the target's vectors locks its row FOR UPDATE before it
    # counts the running swaps onto it.
    assert queries[check].endswith("FOR SHARE")
    # Replaces a finished swap, never a running one.
    assert "WHERE s.status <> $4" in queries[insert]
    assert conn.operations[insert][1] == ("p1", "e5", "bge-m3", "running", 4)


@pytest.mark.asyncio
async def test_a_swap_onto_a_missing_endpoint_is_not_recorded():
    from core.utils.exceptions import NotFoundError
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _SwapConn(endpoint_exists=False)
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    with pytest.raises(NotFoundError) as exc:
        await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="ghost", files_total=0)

    assert exc.value.code == "MODEL_ENDPOINT_NOT_FOUND"
    assert not any("INSERT INTO partition_embedder_swaps" in q for q, _ in conn.operations)


@pytest.mark.asyncio
async def test_a_running_swap_makes_start_return_none():
    from services.persistence.partition_repo import PgPartitionRepository

    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(_SwapConn(inserted=None)))

    assert await repo.start_embedder_swap("p1", source_embedder="e5", target_embedder="bge-m3", files_total=0) is None


@pytest.mark.asyncio
async def test_swap_updates_only_apply_to_a_running_swap_and_only_to_known_columns():
    from services.persistence.partition_repo import PgPartitionRepository

    class _Pool(_FakePool):
        def __init__(self):
            super().__init__(_SwapConn())
            self.calls: list[tuple[str, tuple]] = []

        async def fetchrow(self, query, *params):
            self.calls.append((query, params))
            return None

    pool = _Pool()
    repo = PgPartitionRepository(pool_getter=lambda: pool)

    assert await repo.update_embedder_swap("p1", files_done=3, source_embedder="x", target_embedder="x") is None

    query, params = pool.calls[0]
    assert "SET files_done = $3, updated_at = now()" in query
    assert "WHERE partition = $1 AND status = $2" in query
    assert params == ("p1", "running", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("acquired", [True, False])
async def test_the_runner_lock_is_tried_not_waited_on_and_released_only_when_held(acquired):
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _SwapConn(lock_acquired=acquired)
    pool = _FakePool(conn)
    repo = PgPartitionRepository(pool_getter=lambda: pool, connect=_connect_to(conn))

    async with repo.embedder_swap_runner_lock("p1") as claimed:
        assert claimed is acquired

    queries = [query for query, _ in conn.operations]
    assert any("pg_try_advisory_lock" in q for q in queries)
    assert any("pg_advisory_unlock(" in q for q in queries) is acquired
    # A swap holds its lock for as long as it re-embeds: on a pool connection
    # that would be one request seat fewer all that time.
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_losing_the_runner_lock_session_stops_the_swap_it_was_running():
    """Another process may claim the swap the moment the lock goes."""
    from services.persistence.partition_repo import PgPartitionRepository

    conn = _SwapConn()
    repo = PgPartitionRepository(pool_getter=lambda: None, connect=_connect_to(conn))
    claimed = asyncio.Event()

    async def swap() -> None:
        async with repo.embedder_swap_runner_lock("p1") as running:
            assert running
            claimed.set()
            await asyncio.Event().wait()  # re-embedding, however long it takes

    job = asyncio.create_task(swap())
    await claimed.wait()
    conn.terminated(conn)  # the session went: PostgreSQL released the lock with it

    with pytest.raises(asyncio.CancelledError):
        await job


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_completing_a_swap_switches_the_partition_in_the_same_transaction(running):
    from services.persistence.partition_repo import PgPartitionRepository

    class _CompleteConn(_SwapConn):
        async def fetchrow(self, query: str, *params):
            self.operations.append((query, params))
            if "UPDATE partition_embedder_swaps" in query:
                return {"partition": "p1", "target_embedder": "bge-m3", "status": "completed"} if running else None
            return None

    conn = _CompleteConn()
    repo = PgPartitionRepository(pool_getter=lambda: _FakePool(conn))

    result = await repo.complete_embedder_swap("p1")

    queries = [query for query, _ in conn.operations]
    assert queries[:2] == ["BEGIN", "LOCK TABLE partitions IN ROW EXCLUSIVE MODE"]
    swap_update = next(i for i, q in enumerate(queries) if "UPDATE partition_embedder_swaps" in q)
    assert conn.operations[swap_update][1] == ("p1", "completed", "running")
    partition_updates = [(q, p) for q, p in conn.operations if "UPDATE partitions SET embedder" in q]
    if running:
        assert result["status"] == "completed"
        assert [p for _, p in partition_updates] == [("p1", "bge-m3")]
    else:
        # Cancelled first: the partition keeps its embedder.
        assert result is None
        assert partition_updates == []
