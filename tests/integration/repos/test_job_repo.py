"""Durable indexation job state against a real Postgres (issue #660).

Complements the fake-pool unit tests: these are what actually prove the
``jobs`` migration applied, the CHECK constraint matches the state taxonomy,
and the retention sweep deletes what it claims to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from core.models.catalog import DocumentStatus, IndexationJob
from services.persistence.job_repo import PgJobRepository
from services.storage.postgres_store import PostgresStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
def repo(postgres_store: PostgresStore) -> PgJobRepository:
    return postgres_store.job_repo


async def _user(postgres_store: PostgresStore, name: str = "uploader") -> int:
    # users.created_at is populated by the ORM/model default, not a server
    # default, so a raw INSERT has to supply it.
    return await postgres_store.pool.fetchval(
        "INSERT INTO users (display_name, is_admin, created_at) VALUES ($1, false, now()) RETURNING id",
        name,
    )


def _job(**overrides) -> IndexationJob:
    data = {"id": "task-1", "partition": "tenant-a", "file_id": "file-1", "job_metadata": {"filename": "a.pdf"}}
    data.update(overrides)
    return IndexationJob(**data)


class TestLifecycle:
    async def test_create_then_read_round_trips_every_field(self, repo, postgres_store):
        user_id = await _user(postgres_store)
        await repo.create_job(_job(user_id=user_id))

        job = await repo.get_job("task-1")

        assert job.id == "task-1"
        assert job.status is DocumentStatus.QUEUED
        assert job.partition == "tenant-a"
        assert job.file_id == "file-1"
        assert job.user_id == user_id
        assert job.job_metadata == {"filename": "a.pdf"}
        assert job.error is None

    async def test_full_transition_to_completed_is_durable(self, repo):
        await repo.create_job(_job())
        started = datetime.now(UTC)

        await repo.update_job("task-1", status=DocumentStatus.SERIALIZING, started_at=started)
        await repo.update_job("task-1", status=DocumentStatus.COMPLETED, completed_at=datetime.now(UTC))

        job = await repo.get_job("task-1")
        assert job.status is DocumentStatus.COMPLETED
        assert job.started_at is not None
        assert job.completed_at is not None

    async def test_failure_stores_a_truncated_traceback(self, repo):
        await repo.create_job(_job())

        await repo.update_job("task-1", status=DocumentStatus.FAILED, error="boom\n" + "x" * 200_000)

        job = await repo.get_job("task-1")
        assert job.status is DocumentStatus.FAILED
        assert len(job.error) < 10_000
        assert "truncated" in job.error

    async def test_a_late_failure_cannot_overwrite_a_cancellation(self, repo):
        await repo.create_job(_job())
        await repo.update_job("task-1", status=DocumentStatus.CANCELLED, completed_at=datetime.now(UTC))

        wrote = await repo.mark_failed_if_not_cancelled("task-1", error="boom", completed_at=datetime.now(UTC))

        assert wrote is False
        assert (await repo.get_job("task-1")).status is DocumentStatus.CANCELLED

    async def test_a_failure_lands_when_the_job_was_not_cancelled(self, repo):
        """The write must not depend on the in-memory actor still knowing the task.

        Gating it on the actor's verdict stranded the row in SERIALIZING whenever
        the actor had restarted or evicted the entry, and retention only sweeps
        terminal rows — so the job stayed in the queue views forever (#660).
        """
        await repo.create_job(_job())
        await repo.update_job("task-1", status=DocumentStatus.SERIALIZING, started_at=datetime.now(UTC))

        wrote = await repo.mark_failed_if_not_cancelled(
            "task-1", error="boom\n" + "x" * 200_000, completed_at=datetime.now(UTC)
        )

        assert wrote is True
        job = await repo.get_job("task-1")
        assert job.status is DocumentStatus.FAILED
        assert job.completed_at is not None
        assert len(job.error) < 10_000  # truncation holds on this path too

    async def test_marking_an_unknown_job_failed_reports_no_write(self, repo):
        assert await repo.mark_failed_if_not_cancelled("ghost", error="boom", completed_at=datetime.now(UTC)) is False

    async def test_create_is_idempotent_for_a_redispatched_task(self, repo):
        await repo.create_job(_job())
        await repo.create_job(_job(status=DocumentStatus.CANCELLED))

        assert (await repo.get_job("task-1")).status is DocumentStatus.CANCELLED
        assert await postgres_count(repo) == 1

    async def test_update_of_an_unknown_job_returns_none(self, repo):
        assert await repo.update_job("ghost", status=DocumentStatus.FAILED) is None

    async def test_an_unknown_state_is_rejected_by_the_check_constraint(self, repo):
        # The model validates the enum, so the constraint is the second line of
        # defence — it has to be exercised underneath the repository.
        with pytest.raises(asyncpg.IntegrityConstraintViolationError):
            await repo.pool.execute(
                "INSERT INTO jobs (id, status, partition) VALUES ('bad', 'BOGUS', 'p')",
            )

    async def test_deleting_the_uploader_keeps_the_job_history(self, repo, postgres_store):
        user_id = await _user(postgres_store)
        await repo.create_job(_job(user_id=user_id))

        await postgres_store.pool.execute("DELETE FROM users WHERE id = $1", user_id)

        job = await repo.get_job("task-1")
        assert job is not None
        assert job.user_id is None


class TestQueries:
    async def test_list_jobs_filters_scopes_and_orders_newest_first(self, repo, postgres_store):
        user_id = await _user(postgres_store)
        other_id = await _user(postgres_store, "other")
        now = datetime.now(UTC)
        await repo.create_job(_job(id="old", user_id=user_id, created_at=now - timedelta(hours=1)))
        await repo.create_job(_job(id="new", user_id=user_id, created_at=now))
        await repo.create_job(_job(id="theirs", user_id=other_id))

        mine = await repo.list_jobs(user_id=user_id)

        assert [j.id for j in mine] == ["new", "old"]

    async def test_list_jobs_active_excludes_terminal_jobs(self, repo):
        await repo.create_job(_job(id="running", status=DocumentStatus.CHUNKING))
        await repo.create_job(_job(id="done", status=DocumentStatus.COMPLETED))

        assert [j.id for j in await repo.list_jobs(status="active")] == ["running"]

    async def test_list_jobs_paginates(self, repo):
        for i in range(5):
            await repo.create_job(_job(id=f"t{i}", created_at=datetime.now(UTC) + timedelta(seconds=i)))

        page = await repo.list_jobs(offset=2, limit=2)

        assert [j.id for j in page] == ["t2", "t1"]

    async def test_count_by_status_rolls_up_the_table(self, repo):
        await repo.create_job(_job(id="a", status=DocumentStatus.QUEUED))
        await repo.create_job(_job(id="b", status=DocumentStatus.COMPLETED))
        await repo.create_job(_job(id="c", status=DocumentStatus.COMPLETED))

        assert await repo.count_by_status() == {"QUEUED": 1, "COMPLETED": 2}


class TestRetention:
    async def test_purge_removes_aged_terminal_jobs_only(self, repo):
        stale = datetime.now(UTC) - timedelta(days=30)
        await repo.create_job(_job(id="stale", status=DocumentStatus.COMPLETED, completed_at=stale))
        await repo.create_job(_job(id="fresh", status=DocumentStatus.COMPLETED, completed_at=datetime.now(UTC)))
        # An in-flight job is old but not settled — it must survive.
        await repo.create_job(_job(id="running", status=DocumentStatus.SERIALIZING, created_at=stale))

        purged = await repo.purge_terminal_jobs(older_than_seconds=7 * 24 * 3600, keep_last=1000)

        assert purged == 1
        assert {j.id for j in await repo.list_jobs()} == {"fresh", "running"}

    async def test_purge_caps_the_table_when_jobs_settle_faster_than_the_ttl(self, repo):
        now = datetime.now(UTC)
        for i in range(5):
            await repo.create_job(
                _job(id=f"t{i}", status=DocumentStatus.COMPLETED, completed_at=now + timedelta(seconds=i))
            )

        purged = await repo.purge_terminal_jobs(older_than_seconds=7 * 24 * 3600, keep_last=2)

        assert purged == 3
        assert {j.id for j in await repo.list_jobs()} == {"t4", "t3"}

    async def test_purge_of_an_empty_table_is_a_noop(self, repo):
        assert await repo.purge_terminal_jobs(older_than_seconds=60, keep_last=10) == 0


async def postgres_count(repo: PgJobRepository) -> int:
    return await repo.pool.fetchval("SELECT COUNT(*)::int FROM jobs")
