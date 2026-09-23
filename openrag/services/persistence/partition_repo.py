"""Postgres implementation of :class:`PartitionRepository`.

Manages the ``partitions`` table — the global registry of document
collections. The legacy
:class:`components.indexer.vectordb.utils.PartitionFileManager` exposed
``create_partition``, ``delete_partition``, ``list_partitions``,
``partition_exists``, ``get_partition_file_count``, ``get_total_file_count``
here; all six map onto this class.

Deleting a partition explicitly removes ``files`` before the partition row;
memberships and workspaces use FK cascades. Per-uploader ``file_count`` is
decremented in application code (no SQL trigger) so the books stay balanced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.models.embedder_swap import EmbedderSwapStatus
from core.ports.partition_repo import PartitionRepository
from core.utils.exceptions import NotFoundError, ServiceUnavailableError, ValidationError
from core.utils.logging import get_logger
from services.persistence.file_count import decrement_file_counts

if TYPE_CHECKING:
    import asyncpg
else:  # pragma: no cover - import shape only matters at runtime
    import asyncpg

# Partition columns that reference a pipeline_presets row, mapped to the
# preset_type they point at. Assigning either column must be guarded against a
# concurrent preset delete (see update_partition).
_PRESET_COLUMN_TYPES = {
    "indexation_preset": "indexation",
    "retrieval_preset": "retrieval",
}
# Partition columns that reference a model_endpoints row, mapped to the
# model_type they point at. Both are assignment-validated in
# PartitionService (_validate_chat_llm_ref / _validate_embedder_ref check the
# in-memory catalog) and re-checked here against the DB inside the write's
# transaction. Assigning either must be guarded against a concurrent rename the
# same way a preset assignment is guarded against a concurrent preset delete —
# see update_partition and PgModelEndpointRepository.rename.
_MODEL_ENDPOINT_COLUMN_TYPES = {
    "chat_llm": "llm",
    "embedder": "embedder",
}
# `default` is a virtual name: ModelEndpointService.load_all files the
# is_default=True row under it so a partition can reference "the default
# embedder" without naming it. No model_endpoints row is called that, so the
# existence check has to resolve the alias rather than match on name alone.
_DEFAULT_ENDPOINT_ALIAS = "default"
_ENDPOINT_EXISTS_SQL = (
    "SELECT 1 FROM model_endpoints "
    "WHERE model_type = $2 AND (name = $1 OR ($1 = '" + _DEFAULT_ENDPOINT_ALIAS + "' AND is_default))"
)
# Replaces the `default` alias with the embedder it resolves to, on a partition
# about to receive data (#762). Conditional on the alias, so an explicit
# embedder — or a PATCH that got there first — is left alone and a second
# upload changes nothing. The name is read from the database, not from a
# replica's in-memory catalog: whichever default this sees, the upload is
# dispatched with the same name, so the partition and its vectors agree.
_PIN_DEFAULT_EMBEDDER_SQL = """
    UPDATE partitions
    SET embedder = e.name, updated_at = now()
    FROM model_endpoints e
    WHERE partitions.partition = $1
      AND partitions.embedder = $2
      AND e.model_type = 'embedder' AND e.is_default
    RETURNING partitions.embedder
    """
_PARTITION_UPDATE_COLUMNS = frozenset(
    {
        "description",
        "embedder",
        "indexation_preset",
        "retrieval_preset",
        "dimension",
        "collection_name",
        "chat_history_depth",
        "chat_llm",
        "generation_prompt_names",
    }
)
_PARTITION_OPERATION_LOCK_NAMESPACE = 20260720
_PARTITION_COPY_LOCK_NAMESPACE = 20260921
_EMBEDDER_SWAP_RUNNER_LOCK_NAMESPACE = 20260914
# Shared, and waited for: copies never lock each other out.
_TAKE_SHARED_LOCK_SQL = "SELECT pg_advisory_lock_shared($1::integer, hashtext($2)::integer)"
_RELEASE_SHARED_LOCK_SQL = "SELECT pg_advisory_unlock_shared($1::integer, hashtext($2)::integer)"
# Exclusive, and never waited for: a swap runner only asks whether it is the one.
_TRY_LOCK_SQL = "SELECT pg_try_advisory_lock($1::integer, hashtext($2)::integer)"
_RELEASE_LOCK_SQL = "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)"
_EMBEDDER_SWAP_UPDATE_COLUMNS = frozenset({"status", "files_total", "files_done", "error", "finished_at"})

logger = get_logger()


def _partition_updates(fields: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in fields.items() if key in _PARTITION_UPDATE_COLUMNS}


def _endpoint_refs(updates: dict[str, object]) -> dict[str, str]:
    """Model-endpoint-referencing columns in *updates*, mapped to their model_type.

    A ``None`` value clears the (nullable) column rather than pointing it at a
    name, so it needs no existence check.
    """
    return {
        col: model_type
        for col, model_type in _MODEL_ENDPOINT_COLUMN_TYPES.items()
        if col in updates and updates[col] is not None
    }


class _PartitionOperationGuard:
    def __init__(self, repo: PgPartitionRepository, conn: asyncpg.Connection) -> None:
        self._repo = repo
        self._conn = conn

    async def partition_exists(self, name: str) -> bool:
        return await self._repo._partition_exists_on_conn(self._conn, name)

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        return await self._repo._create_partition_on_conn(
            self._conn,
            name,
            user_id=user_id,
            max_owned=max_owned,
        )

    async def delete_partition(self, name: str) -> bool:
        return await self._repo._delete_partition_on_conn(self._conn, name)

    async def update_partition(self, name: str, **fields: object) -> dict | None:
        return await self._repo._update_partition_on_conn(self._conn, name, **fields)

    async def list_partition_rows(self) -> list[dict]:
        return await self._repo._list_partition_rows_on_conn(self._conn)

    async def pin_default_embedder(self, name: str) -> str | None:
        return await self._repo._pin_default_embedder_on_conn(self._conn, name)


@dataclass(eq=False)
class _SessionHold:
    """One advisory lock held on :class:`_SessionLocks`' connection."""

    # What holds it, for the log a lost session writes.
    what: str
    namespace: int
    name: str
    release_sql: str
    conn: asyncpg.Connection
    task: asyncio.Task
    # The task's pending cancellations when the hold was taken, to tell ours apart.
    cancelling: int
    lost: bool = False


class _SessionLocks:
    """The advisory locks of this process's long jobs, on a connection of their own.

    A copy or an embedder swap runs for as long as it re-embeds — minutes, and
    for a swap however long a partition takes — so a pool connection per job
    would let a few of them starve every request. A session can hold a shared
    lock several times over: each job takes and releases one hold.

    Losing the session releases its holds at once. Taking them back later would
    leave a gap an embedder change could slip through, or a second runner claim
    a swap this one is still running, so the jobs they protected are cancelled
    instead.
    """

    def __init__(self, connect: Callable[[], Awaitable[asyncpg.Connection]]) -> None:
        self._connect = connect
        self._conn: asyncpg.Connection | None = None
        self._holds: set[_SessionHold] = set()
        self._mutex = asyncio.Lock()

    async def hold(self, namespace: int, name: str, *, what: str) -> _SessionHold:
        """Take a shared lock, waiting for it, and keep it until released."""
        hold = await self._granted(namespace, name, _TAKE_SHARED_LOCK_SQL, _RELEASE_SHARED_LOCK_SQL, what=what)
        assert hold is not None  # a waited-for lock is always granted in the end
        return hold

    async def try_hold(self, namespace: int, name: str, *, what: str) -> _SessionHold | None:
        """Take an exclusive lock if it is free, or answer ``None`` at once."""
        return await self._granted(namespace, name, _TRY_LOCK_SQL, _RELEASE_LOCK_SQL, what=what)

    async def _granted(
        self, namespace: int, name: str, take_sql: str, release_sql: str, *, what: str
    ) -> _SessionHold | None:
        task = asyncio.current_task()
        assert task is not None
        # Shielded: a lock granted to a caller cancelled meanwhile would stay
        # on the session with no hold to release it.
        taking = asyncio.ensure_future(self._take(namespace, name, take_sql, release_sql, task, what))
        try:
            return await asyncio.shield(taking)
        except asyncio.CancelledError:
            await asyncio.shield(self._give_back(taking))
            raise

    async def _take(
        self, namespace: int, name: str, take_sql: str, release_sql: str, task: asyncio.Task, what: str
    ) -> _SessionHold | None:
        async with self._mutex:
            try:
                conn = await self._connection()
                granted = await self._ask(conn, take_sql, namespace, name)
            except Exception:  # noqa: BLE001 - most likely a lost session: retry once on a new one
                self._discard()
                conn = await self._connection()
                granted = await self._ask(conn, take_sql, namespace, name)
            if not granted:
                return None
            hold = _SessionHold(what, namespace, name, release_sql, conn, task, task.cancelling())
            self._holds.add(hold)
            return hold

    @staticmethod
    async def _ask(conn: asyncpg.Connection, take_sql: str, namespace: int, name: str) -> bool:
        """Ask for the lock; ``False`` only from a try-lock someone else holds."""
        if "pg_try" not in take_sql:
            await conn.execute(take_sql, namespace, name)
            return True
        return bool(await conn.fetchval(take_sql, namespace, name))

    async def _give_back(self, taking: asyncio.Future[_SessionHold | None]) -> None:
        """Release the lock a cancelled caller was granted all the same."""
        try:
            hold = await taking
        except Exception:  # noqa: BLE001 - never granted: nothing to release
            return
        if hold is None:
            return
        self.forget(hold)
        await self.release(hold)

    def forget(self, hold: _SessionHold) -> None:
        """Stop guarding *hold*: its job is over, and must no longer be cancelled."""
        self._holds.discard(hold)

    async def release(self, hold: _SessionHold) -> None:
        async with self._mutex:
            if hold.conn is not self._conn:
                return  # its session is gone, and the hold with it
            try:
                await hold.conn.execute(hold.release_sql, hold.namespace, hold.name)
            except Exception as exc:  # noqa: BLE001 - dropping the session releases the hold anyway
                logger.bind(partition=hold.name, error=str(exc)).warning("Dropped the job-lock connection")
                self._discard()

    async def close(self) -> None:
        async with self._mutex:
            if self._conn is not None:
                await self._conn.close()
            self._conn = None

    def _discard(self) -> None:
        """Drop the session, and every job holding a lock on it."""
        conn = self._conn
        if conn is not None:
            self._lose(conn)
            conn.terminate()

    def _lose(self, conn: asyncpg.Connection) -> None:
        """Cancel the jobs whose holds went with *conn*'s session."""
        if self._conn is conn:
            self._conn = None
        for hold in [hold for hold in self._holds if hold.conn is conn]:
            self._holds.discard(hold)
            hold.lost = True
            hold.task.cancel()
            logger.bind(partition=hold.name).warning(f"Lost the lock of a {hold.what}: stopping it")

    async def _connection(self) -> asyncpg.Connection:
        if self._conn is not None and self._conn.is_closed():
            self._discard()
        if self._conn is None:
            conn = await self._connect()
            conn.add_termination_listener(self._lose)
            self._conn = conn
        return self._conn


class PgPartitionRepository(PartitionRepository):
    """asyncpg-backed implementation of :class:`PartitionRepository`."""

    def __init__(
        self,
        pool_getter: Callable[[], asyncpg.Pool],
        connect: Callable[[], Awaitable[asyncpg.Connection]] | None = None,
    ) -> None:
        self._pool_getter = pool_getter
        # Opens the connection the locks of long jobs live on, outside the pool.
        self._session_locks = _SessionLocks(connect) if connect is not None else None

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool_getter()

    @asynccontextmanager
    async def partition_operation_lock(self, name: str) -> AsyncIterator[_PartitionOperationGuard]:
        """Hold a cross-process fence for partition deletes and upload admission."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_lock($1::integer, hashtext($2)::integer)",
                _PARTITION_OPERATION_LOCK_NAMESPACE,
                name,
            )
            try:
                yield _PartitionOperationGuard(self, conn)
            finally:
                await conn.execute(
                    "SELECT pg_advisory_unlock($1::integer, hashtext($2)::integer)",
                    _PARTITION_OPERATION_LOCK_NAMESPACE,
                    name,
                )

    @asynccontextmanager
    async def copy_lock(self, name: str) -> AsyncIterator[None]:
        """Mark a copy into partition *name* as in flight, for :meth:`copy_in_progress`.

        Shared: copies never wait on each other, and uploads never take it.
        Held outside the request pool, see :class:`_SessionLocks`. Raises
        :class:`ServiceUnavailableError` from the copy if the lock is lost.
        """
        if self._session_locks is None:
            raise RuntimeError("PgPartitionRepository was built without a connect callable for copy locks.")
        hold = await self._session_locks.hold(_PARTITION_COPY_LOCK_NAMESPACE, name, what="copy")
        try:
            yield
        except asyncio.CancelledError:
            if hold.lost and hold.task.uncancel() <= hold.cancelling:
                raise ServiceUnavailableError(
                    f"The copy into partition '{name}' was stopped: it lost the lock that keeps "
                    "the partition's embedder from changing meanwhile. Retry the copy.",
                    code="COPY_INTERRUPTED",
                ) from None
            raise
        finally:
            # Before any await, so a copy that finished is never cancelled.
            self._session_locks.forget(hold)
            # Shielded: a hold left behind would block the partition's
            # embedder changes until the process exits.
            await asyncio.shield(self._session_locks.release(hold))

    async def aclose(self) -> None:
        if self._session_locks is not None:
            await self._session_locks.close()

    async def copy_in_progress(self, name: str) -> bool:
        """Whether a copy into partition *name* holds :meth:`copy_lock`. Never waits."""
        # Released as soon as the statement's own transaction ends.
        acquired = await self.pool.fetchval(
            "SELECT pg_try_advisory_xact_lock($1::integer, hashtext($2)::integer)",
            _PARTITION_COPY_LOCK_NAMESPACE,
            name,
        )
        return not acquired

    @asynccontextmanager
    async def embedder_swap_runner_lock(self, partition: str) -> AsyncIterator[bool]:
        """Claim the one runner of *partition*'s swap, for as long as the job runs.

        Session-level, so a runner that dies releases it with its connection —
        and held outside the request pool, see :class:`_SessionLocks`: a swap
        keeps it for however long re-embedding the partition takes, which on a
        request connection would be a seat fewer for every upload and query
        meanwhile. Losing the session cancels the job, since another process
        may claim the swap the moment the lock goes; the row still says
        running, so the swap resumes rather than ending there.
        """
        if self._session_locks is None:
            raise RuntimeError("PgPartitionRepository was built without a connect callable for swap runner locks.")
        hold = await self._session_locks.try_hold(_EMBEDDER_SWAP_RUNNER_LOCK_NAMESPACE, partition, what="embedder swap")
        if hold is None:
            yield False
            return
        try:
            yield True
        finally:
            # Before any await, so a swap that finished is never cancelled.
            self._session_locks.forget(hold)
            # Shielded: a hold left behind would keep every other process from
            # running this partition's swaps until this one exits.
            await asyncio.shield(self._session_locks.release(hold))

    # ── PartitionRepository port methods ─────────────────────────────

    async def create_partition(self, name: str, user_id: int | None = None, *, max_owned: int | None = None) -> dict:
        """Insert a partition row and grant the creator owner membership.

        Existing partitions are not treated as successful creates. The service
        layer needs that distinction so it does not update preset/config fields
        for a partition it did not create.
        """
        async with self.pool.acquire() as conn:
            return await self._create_partition_on_conn(conn, name, user_id=user_id, max_owned=max_owned)

    async def _create_partition_on_conn(
        self,
        conn: asyncpg.Connection,
        name: str,
        user_id: int | None = None,
        *,
        max_owned: int | None = None,
    ) -> dict:
        async with conn.transaction():
            if user_id is not None and max_owned is not None and max_owned >= 0:
                await conn.execute("SELECT pg_advisory_xact_lock($1::bigint)", user_id)
            row = await conn.fetchrow(
                "SELECT * FROM partitions WHERE partition = $1",
                name,
            )
            if row is not None:
                raise ValidationError(
                    f"Partition '{name}' already exists.",
                    status_code=409,
                    code="PARTITION_EXISTS",
                )
            if user_id is not None and max_owned is not None and max_owned >= 0:
                owned = await conn.fetchval(
                    """
                    SELECT COUNT(*)::int FROM partition_memberships
                    WHERE user_id = $1 AND role = 'owner'
                    """,
                    user_id,
                )
                if owned >= max_owned:
                    raise ValidationError(
                        f"Partition limit reached ({max_owned}). Contact an administrator.",
                        status_code=403,
                        code="PARTITION_LIMIT_EXCEEDED",
                    )
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO partitions (partition, created_at)
                    VALUES ($1, NOW())
                    RETURNING *
                    """,
                    name,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ValidationError(
                    f"Partition '{name}' already exists.",
                    status_code=409,
                    code="PARTITION_EXISTS",
                ) from exc
            if user_id is not None:
                await conn.execute(
                    """
                    INSERT INTO partition_memberships
                        (partition_name, user_id, role, added_at)
                    VALUES ($1, $2, 'owner', NOW())
                    ON CONFLICT (partition_name, user_id) DO NOTHING
                    """,
                    name,
                    user_id,
                )
        return self._row_to_dict(row)

    async def get_partition(self, name: str) -> dict | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM partitions WHERE partition = $1",
            name,
        )
        return self._row_to_dict(row) if row else None

    async def list_partitions(self) -> list[dict]:
        rows = await self.pool.fetch("SELECT * FROM partitions ORDER BY created_at")
        return [self._row_to_dict(r) for r in rows]

    async def delete_partition(self, name: str) -> bool:
        """Delete a partition + its files, memberships, and workspaces.

        ``files.partition_name`` has no ``ON DELETE CASCADE`` (the legacy
        ORM relied on SQLAlchemy's Python-side cascade), so we delete file
        rows explicitly before the partition. ``workspace_files.file_id``
        cascades, so workspace links clean up with the files.
        ``partition_memberships`` and ``workspaces`` cascade from the
        partition row.

        Counter updates are derived from the rows actually deleted and remain
        clamped at zero so retries and concurrent deletion paths stay safe.
        """
        async with self.pool.acquire() as conn:
            return await self._delete_partition_on_conn(conn, name)

    async def _delete_partition_on_conn(self, conn: asyncpg.Connection, name: str) -> bool:
        async with conn.transaction():
            partition = await conn.fetchrow(
                "SELECT partition FROM partitions WHERE partition = $1 FOR UPDATE",
                name,
            )
            if partition is None:
                logger.bind(
                    operation="delete_partition",
                    deleted_rows=0,
                    counters_adjusted=0,
                ).debug("Catalog partition deletion accounted")
                return False
            deleted_rows = await conn.fetch(
                "DELETE FROM files WHERE partition_name = $1 RETURNING created_by",
                name,
            )
            counters_adjusted = await decrement_file_counts(conn, deleted_rows)
            await conn.execute(
                "DELETE FROM partitions WHERE partition = $1",
                name,
            )
            logger.bind(
                operation="delete_partition",
                deleted_rows=len(deleted_rows),
                counters_adjusted=counters_adjusted,
            ).debug("Catalog partition deletion accounted")
            return True

    async def partition_exists(self, name: str) -> bool:
        return await self._partition_exists_on_conn(
            self.pool,
            name,
        )

    async def _partition_exists_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool, name: str) -> bool:
        return await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM partitions WHERE partition = $1)",
            name,
        )

    # ── Phase 14 — full config row methods ───────────────────────────

    async def get_partition_row(self, name: str) -> dict | None:
        row = await self._get_partition_row_on_conn(
            self.pool,
            name,
        )
        return row

    async def _get_partition_row_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool, name: str) -> dict | None:
        row = await conn.fetchrow(
            "SELECT * FROM partitions WHERE partition = $1",
            name,
        )
        return self._row_to_full_dict(row) if row else None

    async def list_partition_rows(self) -> list[dict]:
        return await self._list_partition_rows_on_conn(self.pool)

    async def _list_partition_rows_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool) -> list[dict]:
        rows = await conn.fetch(
            "SELECT * FROM partitions ORDER BY created_at",
        )
        return [self._row_to_full_dict(r) for r in rows]

    async def start_embedder_swap(
        self, partition: str, *, source_embedder: str, target_embedder: str, files_total: int
    ) -> dict | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # ROW EXCLUSIVE conflicts with the SHARE lock an endpoint delete
                # or rename takes on ``partitions`` first (see
                # PgModelEndpointRepository), and is taken in the same order:
                # partitions, then model_endpoints. Whichever commits first is
                # what the other sees. The target's row FOR SHARE: an edit that
                # changes its vectors locks it FOR UPDATE, then counts the swaps
                # onto it, so this swap is either counted or starts after the edit.
                await conn.execute("LOCK TABLE partitions IN ROW EXCLUSIVE MODE")
                exists = await conn.fetchval(
                    "SELECT 1 FROM model_endpoints WHERE name = $1 AND model_type = 'embedder' FOR SHARE",
                    target_embedder,
                )
                if not exists:
                    raise NotFoundError(
                        f"Embedder endpoint '{target_embedder}' not found.",
                        code="MODEL_ENDPOINT_NOT_FOUND",
                    )
                row = await conn.fetchrow(
                    """
                    INSERT INTO partition_embedder_swaps AS s
                        (partition, source_embedder, target_embedder, status, files_total, files_done,
                         error, started_at, updated_at, finished_at)
                    VALUES ($1, $2, $3, $4, $5, 0, NULL, now(), now(), NULL)
                    ON CONFLICT (partition) DO UPDATE SET
                        source_embedder = EXCLUDED.source_embedder,
                        target_embedder = EXCLUDED.target_embedder,
                        status = EXCLUDED.status,
                        files_total = EXCLUDED.files_total,
                        files_done = 0,
                        error = NULL,
                        started_at = now(),
                        updated_at = now(),
                        finished_at = NULL
                    WHERE s.status <> $4
                    RETURNING *
                    """,
                    partition,
                    source_embedder,
                    target_embedder,
                    EmbedderSwapStatus.RUNNING.value,
                    files_total,
                )
        return dict(row) if row is not None else None

    async def get_embedder_swap(self, partition: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM partition_embedder_swaps WHERE partition = $1", partition)
        return dict(row) if row is not None else None

    async def list_embedder_swaps(self, status: str | None = None) -> list[dict]:
        if status is None:
            rows = await self.pool.fetch("SELECT * FROM partition_embedder_swaps ORDER BY started_at")
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM partition_embedder_swaps WHERE status = $1 ORDER BY started_at",
                status,
            )
        return [dict(r) for r in rows]

    async def update_embedder_swap(self, partition: str, **fields: object) -> dict | None:
        updates = {k: v for k, v in fields.items() if k in _EMBEDDER_SWAP_UPDATE_COLUMNS}
        sets = [f"{column} = ${i}" for i, column in enumerate(updates, start=3)]
        row = await self.pool.fetchrow(
            f"""
            UPDATE partition_embedder_swaps
            SET {", ".join([*sets, "updated_at = now()"])}
            WHERE partition = $1 AND status = $2
            RETURNING *
            """,
            partition,
            EmbedderSwapStatus.RUNNING.value,
            *updates.values(),
        )
        return dict(row) if row is not None else None

    async def complete_embedder_swap(self, partition: str) -> dict | None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Same lock order as start_embedder_swap and the endpoint
                # delete/rename guards: partitions first.
                await conn.execute("LOCK TABLE partitions IN ROW EXCLUSIVE MODE")
                swap = await conn.fetchrow(
                    """
                    UPDATE partition_embedder_swaps
                    SET status = $2, finished_at = now(), updated_at = now()
                    WHERE partition = $1 AND status = $3
                    RETURNING *
                    """,
                    partition,
                    EmbedderSwapStatus.COMPLETED.value,
                    EmbedderSwapStatus.RUNNING.value,
                )
                if swap is None:
                    return None
                await conn.execute(
                    "UPDATE partitions SET embedder = $2, updated_at = now() WHERE partition = $1",
                    partition,
                    swap["target_embedder"],
                )
        return dict(swap)

    async def update_partition(self, name: str, **fields: object) -> dict | None:
        """Update a partition's config columns.

        When the update assigns a preset column (``indexation_preset`` /
        ``retrieval_preset``) or ``chat_llm``, the write and a DB-authoritative
        existence check run in one transaction that touches ``partitions``
        before ``pipeline_presets`` / ``model_endpoints`` — the same lock order
        :meth:`PgPresetRepository.delete` and :meth:`PgModelEndpointRepository.
        rename` use (both ``LOCK`` ``partitions`` ``IN SHARE MODE`` first). That
        makes an assign and a concurrent delete/rename of the same reference
        serialize without deadlocking, so a partition can never end up pointing
        at a preset or model endpoint that no longer exists under that name:

        * if this UPDATE commits first, the delete/rename's own guard against
          ``partitions`` (a ``COUNT`` for presets, the ``SHARE`` lock itself for
          renames) sees the reference and blocks or refuses accordingly;
        * if the delete/rename commits first, this UPDATE blocks on its
          ``SHARE``-conflicting write, then the follow-up ``SELECT`` sees the
          vanished name and the transaction rolls the write back (raising
          ``PRESET_NOT_FOUND`` / ``MODEL_ENDPOINT_NOT_FOUND``) — instead of
          silently writing back a name a concurrent rename already moved on
          from, which is what a validate-in-memory-then-blind-UPDATE sequence
          could otherwise do.

        ``embedder`` takes the same guard as ``chat_llm``, and needs it more:
        a ``chat_llm`` that goes stale falls back to the default LLM at request
        time, whereas an ``embedder`` that names nothing is a hard failure on
        every upload and every query in that partition.
        """
        updates = _partition_updates(fields)
        if updates:
            preset_refs = {col: _PRESET_COLUMN_TYPES[col] for col in updates if col in _PRESET_COLUMN_TYPES}
            endpoint_refs = _endpoint_refs(updates)
            if preset_refs or endpoint_refs:
                async with self.pool.acquire() as conn:
                    return await self._update_partition_on_conn(conn, name, **fields)
        return await self._update_partition_on_conn(self.pool, name, **fields)

    async def _update_partition_on_conn(
        self,
        conn: asyncpg.Connection | asyncpg.Pool,
        name: str,
        **fields: object,
    ) -> dict | None:
        updates = _partition_updates(fields)
        if not updates:
            return await self._get_partition_row_on_conn(conn, name)

        params: list = [name]
        sets: list[str] = []
        for col, val in updates.items():
            idx = len(params) + 1
            sets.append(f"{col} = ${idx}::jsonb" if col == "generation_prompt_names" else f"{col} = ${idx}")
            params.append(val)
        sql = f"UPDATE partitions SET {', '.join(sets)}, updated_at = now() WHERE partition = $1 RETURNING *"

        preset_refs = {col: _PRESET_COLUMN_TYPES[col] for col in updates if col in _PRESET_COLUMN_TYPES}
        endpoint_refs = _endpoint_refs(updates)
        if not preset_refs and not endpoint_refs:
            row = await conn.fetchrow(sql, *params)
            return self._row_to_full_dict(row) if row else None

        transaction = getattr(conn, "transaction", None)
        if transaction is None:
            raise TypeError("preset/model-endpoint reference updates require a connection transaction")
        async with transaction():
            row = await conn.fetchrow(sql, *params)
            if row is None:
                return None
            for col, preset_type in preset_refs.items():
                exists = await conn.fetchval(
                    "SELECT 1 FROM pipeline_presets WHERE name = $1 AND preset_type = $2",
                    updates[col],
                    preset_type,
                )
                if not exists:
                    raise ValidationError(
                        f"{preset_type.capitalize()} preset '{updates[col]}' does not exist.",
                        code="PRESET_NOT_FOUND",
                    )
            for col, model_type in endpoint_refs.items():
                exists = await conn.fetchval(
                    _ENDPOINT_EXISTS_SQL,
                    updates[col],
                    model_type,
                )
                if not exists:
                    raise ValidationError(
                        f"{model_type.upper()} endpoint '{updates[col]}' referenced by {col} not found.",
                        code="MODEL_ENDPOINT_NOT_FOUND",
                    )
            return self._row_to_full_dict(row)

    async def pin_default_embedder(self, name: str) -> str | None:
        """Resolve a partition's ``default`` embedder alias to the endpoint it names.

        Returns the partition's embedder afterwards: the endpoint it is now
        pinned to, the explicit name it already had, ``"default"`` when no
        default embedder exists to resolve to, or ``None`` when the partition
        does not exist.
        """
        return await self._pin_default_embedder_on_conn(self.pool, name)

    async def _pin_default_embedder_on_conn(self, conn: asyncpg.Connection | asyncpg.Pool, name: str) -> str | None:
        pinned = await conn.fetchval(_PIN_DEFAULT_EMBEDDER_SQL, name, _DEFAULT_ENDPOINT_ALIAS)
        if pinned is not None:
            return pinned
        return await conn.fetchval("SELECT embedder FROM partitions WHERE partition = $1", name)

    # ── Legacy method names used by the Phase 7C shim ────────────────

    async def get_partition_file_count(self, partition: str) -> int:
        """Number of indexed files in one partition (powers ``document_count``)."""
        return await self.pool.fetchval(
            "SELECT COUNT(*)::int FROM files WHERE partition_name = $1",
            partition,
        )

    async def count_files_by_partition(self) -> dict[str, int]:
        """Return a ``{partition_name: file_count}`` map for all partitions in one query."""
        rows = await self.pool.fetch(
            "SELECT partition_name, COUNT(*)::int AS n FROM files GROUP BY partition_name",
        )
        return {r["partition_name"]: r["n"] for r in rows}

    async def get_total_file_count(self) -> int:
        """TODO(phase-9): remove."""
        return await self.pool.fetchval("SELECT COUNT(*)::int FROM files")

    # ── Row → dict helper ────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: asyncpg.Record) -> dict:
        """Shape mirrors the legacy ``Partition.to_dict()`` ORM helper."""
        created = row["created_at"]
        return {
            "partition": row["partition"],
            "created_at": created.isoformat() if created else None,
        }

    @staticmethod
    def _row_to_full_dict(row: asyncpg.Record) -> dict:
        """Full partition row including all Phase 14 config columns."""
        return {
            "partition": row["partition"],
            "description": row["description"],
            "embedder": row["embedder"],
            "indexation_preset": row["indexation_preset"],
            "retrieval_preset": row["retrieval_preset"],
            # Never written by any code path — it sits at its server_default of
            # 1024 for the life of the row. Kept as the hook a per-partition
            # collection topology would need, but the API reports the live
            # collection's dimension instead (see
            # PartitionService._live_vector_dimension, #762 G).
            "dimension": row["dimension"],
            "collection_name": row["collection_name"],
            "chat_history_depth": row["chat_history_depth"],
            "chat_llm": row["chat_llm"],
            "generation_prompt_names": row["generation_prompt_names"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


__all__ = ["PgPartitionRepository"]
