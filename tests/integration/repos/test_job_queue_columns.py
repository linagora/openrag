"""Postgres coverage for indexing job storage shared by Ray and the queue."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from core.models.catalog import DocumentStatus, IndexationJob
from services.persistence.schema import metadata
from sqlalchemy import create_engine

from .conftest import _admin_dsn, _connect_admin
from .test_similarity_threshold_migration import _alembic_config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

PREVIOUS = "f4a5b6c7d8e9"
REVISION = "d9f1c27b5e40"


@pytest_asyncio.fixture(loop_scope="session")
async def migration_database():
    admin = await _connect_admin()
    assert admin is not None
    name = f"openrag_queue_migration_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        cfg = _alembic_config(name)
        dsn = _admin_dsn().rsplit("/", 1)[0] + f"/{name}"
        yield cfg, dsn
    finally:
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def _queue_indexes(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(
        """
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'jobs' AND indexname LIKE 'ix_jobs_queue_%'
        """
    )
    return {row["indexname"]: row["indexdef"] for row in rows}


async def test_upgrade_downgrade_upgrade_preserves_ray_jobs(migration_database):
    cfg, dsn = migration_database
    await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("INSERT INTO jobs (id, partition, status) VALUES ('old-ray', 'default', 'QUEUED')")
        await conn.execute("INSERT INTO partitions (partition, created_at) VALUES ('queue-test', now())")
        await conn.execute("INSERT INTO files (file_id, partition_name) VALUES ('existing-file', 'queue-test')")
        await asyncio.to_thread(command.upgrade, cfg, REVISION)

        row = await conn.fetchrow(
            "SELECT lane, attempt, max_attempts, cancel_requested, available_at FROM jobs WHERE id = 'old-ray'"
        )
        assert row is not None
        assert (row["lane"], row["attempt"], row["max_attempts"], row["cancel_requested"]) == (None, 0, 3, False)
        assert row["available_at"] is not None
        assert await conn.fetchrow("SELECT indexed_by_job FROM files WHERE file_id = 'existing-file'") == (None,)
        await conn.execute("UPDATE files SET indexed_by_job = 'queue-job' WHERE file_id = 'existing-file'")
        assert await conn.fetchval("SELECT indexed_by_job FROM files WHERE file_id = 'existing-file'") == "queue-job"
        indexes = await _queue_indexes(conn)
        assert "(lane, priority DESC, available_at)" in indexes["ix_jobs_queue_claim"]
        assert "QUEUED" in indexes["ix_jobs_queue_claim"]
        assert "lane IS NOT NULL" in indexes["ix_jobs_queue_running_user"]
        options = await conn.fetchval("SELECT reloptions FROM pg_class WHERE oid = 'jobs'::regclass")
        assert {"fillfactor=80", "autovacuum_vacuum_scale_factor=0.01"} <= set(options)

        await asyncio.to_thread(command.downgrade, cfg, PREVIOUS)
        assert not await _queue_indexes(conn)
        assert await conn.fetchval("SELECT status FROM jobs WHERE id = 'old-ray'") == "QUEUED"
        with pytest.raises(asyncpg.UndefinedColumnError):
            await conn.fetchval("SELECT lane FROM jobs")
        with pytest.raises(asyncpg.UndefinedColumnError):
            await conn.fetchval("SELECT indexed_by_job FROM files")

        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await _queue_indexes(conn) == indexes
        assert await conn.fetchval("SELECT lane FROM jobs WHERE id = 'old-ray'") is None
        assert await conn.fetchrow("SELECT indexed_by_job FROM files WHERE file_id = 'existing-file'") == (None,)
    finally:
        await conn.close()


async def test_lane_constraint_accepts_known_lanes_and_null_after_migration(migration_database):
    cfg, dsn = migration_database
    await asyncio.to_thread(command.upgrade, cfg, REVISION)
    conn = await asyncpg.connect(dsn)
    try:
        insert = "INSERT INTO jobs (id, partition, status, lane) VALUES ($1, 'default', 'QUEUED', $2)"
        for lane in (None, "cpu", "gpu", "remote"):
            await conn.execute(insert, f"job-{lane}", lane)
        with pytest.raises(asyncpg.CheckViolationError, match="ck_jobs_lane"):
            await conn.execute(insert, "job-unknown", "unknown")
        assert await conn.fetchval(
            "SELECT convalidated FROM pg_constraint WHERE conrelid = 'jobs'::regclass AND conname = 'ck_jobs_lane'"
        )
    finally:
        await conn.close()


async def test_upgrade_after_metadata_created_columns_and_indexes(migration_database):
    cfg, dsn = migration_database

    def create_schema() -> None:
        engine = create_engine(cfg.get_main_option("sqlalchemy.url"))
        try:
            metadata.create_all(engine)
        finally:
            engine.dispose()

    await asyncio.to_thread(create_schema)
    await asyncio.to_thread(command.stamp, cfg, PREVIOUS)
    conn = await asyncpg.connect(dsn)
    try:
        indexes_before = await _queue_indexes(conn)
        assert set(indexes_before) == {"ix_jobs_queue_claim", "ix_jobs_queue_running_user"}
        await conn.execute(
            "INSERT INTO jobs (id, partition, status, lane) VALUES ('existing', 'default', 'QUEUED', 'cpu')"
        )
        with pytest.raises(asyncpg.CheckViolationError, match="ck_jobs_lane"):
            await conn.execute("UPDATE jobs SET lane = 'unknown' WHERE id = 'existing'")
        await asyncio.to_thread(command.upgrade, cfg, REVISION)
        assert await _queue_indexes(conn) == indexes_before
        assert await conn.fetchval("SELECT lane FROM jobs WHERE id = 'existing'") == "cpu"
    finally:
        await conn.close()


async def test_upgrade_rebuilds_an_invalid_queue_index(migration_database):
    cfg, dsn = migration_database
    await asyncio.to_thread(command.upgrade, cfg, PREVIOUS)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO jobs (id, partition, status) VALUES "
            "('first', 'default', 'QUEUED'), ('second', 'default', 'QUEUED')"
        )
        # A failed concurrent build leaves an invalid catalog entry. Use a
        # uniqueness failure to produce one without timing a cancellation.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_jobs_queue_claim ON jobs (status)")
        validity_sql = "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_jobs_queue_claim'::regclass"
        assert await conn.fetchval(validity_sql) is False

        await asyncio.to_thread(command.upgrade, cfg, REVISION)

        assert await conn.fetchval(validity_sql) is True
        assert "(lane, priority DESC, available_at)" in (await _queue_indexes(conn))["ix_jobs_queue_claim"]
    finally:
        await conn.close()


async def test_queue_fields_round_trip_and_ray_job_updates_preserve_them(postgres_store):
    repo = postgres_store.job_repo
    now = datetime.now(UTC)
    queue_fields = {
        "lane": "gpu",
        "payload": {"replace": True, "metadata": {"title": "report"}},
        "object_key": "uploads/report",
        "attempt": 2,
        "max_attempts": 4,
        "lease_owner": "worker",
        "lease_expires_at": now + timedelta(seconds=90),
        "heartbeat_at": now,
        "cancel_requested": True,
        "available_at": now,
        "priority": 5,
        "superseded_by": "replacement",
    }
    inserted = await repo.upsert_job(IndexationJob(id="queue", status=DocumentStatus.SERIALIZING, **queue_fields))
    await repo.upsert_job(IndexationJob(id="queue", status=DocumentStatus.SERIALIZING))
    fetched = await repo.get_job("queue")
    assert fetched is not None
    for field, value in queue_fields.items():
        assert getattr(inserted, field) == getattr(fetched, field) == value


async def test_ray_reconciler_leaves_queued_and_running_queue_jobs_alone(postgres_store):
    repo = postgres_store.job_repo
    jobs = [
        IndexationJob(id="ray-orphan"),
        IndexationJob(id="ray-active"),
        IndexationJob(id="ray-settled", status=DocumentStatus.COMPLETED),
        IndexationJob(id="queue-waiting", lane="cpu"),
        IndexationJob(id="queue-running", lane="gpu", status=DocumentStatus.SERIALIZING),
    ]
    for job in jobs:
        await repo.upsert_job(job)
    cutoff = datetime.now(UTC) - timedelta(minutes=5)
    await postgres_store.job_repo.pool.execute("UPDATE jobs SET updated_at = $1", cutoff - timedelta(seconds=1))
    failed = await repo.fail_orphaned_jobs(active_ids=["ray-active"], error="restart", before=cutoff)

    assert failed == 1
    assert {job.id: job.status for job in await repo.get_jobs([job.id for job in jobs])} == {
        "ray-orphan": DocumentStatus.FAILED,
        "ray-active": DocumentStatus.QUEUED,
        "ray-settled": DocumentStatus.COMPLETED,
        "queue-waiting": DocumentStatus.QUEUED,
        "queue-running": DocumentStatus.SERIALIZING,
    }
