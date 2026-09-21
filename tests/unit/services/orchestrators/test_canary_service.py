"""Unit tests for the synthetic canary run and its scheduler."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from core.config.canary import CanaryConfig
from core.config.root import Settings
from core.models.chunk import Chunk
from core.models.user import User
from core.observability.canary import (
    CANARY_PARTITION,
    CANARY_USER_EMAIL,
    CanaryRunResult,
    CanaryStage,
)
from core.observability.monitoring import CanaryMetrics
from prometheus_client import CollectorRegistry
from services.orchestrators.canary_service import (
    CanaryScheduler,
    CanaryService,
    canary_file_created_at,
)

NOW = 1_800_000_000.0
CANARY_USER_ID = 99


class Clock:
    """Monotonic clock that moves one second per reading, so every stage has a duration."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class World:
    """The catalog and vector store the fakes share."""

    def __init__(self) -> None:
        self.catalog: set[tuple[str, str]] = set()
        self.vectors: set[tuple[str, str]] = set()


class FakeIndexing:
    def __init__(self, world, *, states=("QUEUED", "SERIALIZING", "COMPLETED"), error=None, add_error=None):
        self.world = world
        self.states = list(states)
        self.error = error
        self.add_error = add_error
        self.added: list[dict] = []
        self.deleted: list[tuple[str, str]] = []
        self.keep_catalog_row = False
        self.keep_vectors = False

    async def add_file(self, **kwargs):
        if self.add_error is not None:
            raise self.add_error
        self.added.append({**kwargs, "text": Path(kwargs["file_path"]).read_text(encoding="utf-8")})
        key = (kwargs["partition"], kwargs["file_id"])
        self.world.catalog.add(key)
        self.world.vectors.add(key)
        return "task-1"

    async def get_task_state(self, task_id):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def get_task_error(self, task_id):
        return self.error

    async def delete_file(self, file_id, partition):
        self.deleted.append((file_id, partition))
        if not self.keep_catalog_row:
            self.world.catalog.discard((partition, file_id))
        if not self.keep_vectors:
            self.world.vectors.discard((partition, file_id))


class FakeRetrieval:
    def __init__(self, world, *, hit=True):
        self.world = world
        self.hit = hit
        self.calls: list[tuple[list[str], str]] = []

    async def retrieve(self, *, partitions, query, top_k=None, filter_params=None):
        self.calls.append((partitions, query.query))
        if not self.hit:
            return [Chunk(document_id="someone-else", partition=partitions[0], text="unrelated")]
        return [
            Chunk(document_id=file_id, partition=partition, text="OpenRag synthetic canary document.")
            for partition, file_id in sorted(self.world.catalog)
            if partition in partitions
        ]


class FakePartitions:
    def __init__(self, world, *, exists=False, members=None):
        self.world = world
        self.exists = exists
        self.members: dict[int, str] = dict(members or {})
        self.created: list[dict] = []
        self.member_changes: list[tuple[str, int, str]] = []
        self.updates: list[dict] = []

    async def partition_exists(self, partition):
        return self.exists

    async def create_partition(self, partition, *, user_id, description, system):
        self.exists = True
        self.members[user_id] = "owner"
        self.created.append({"partition": partition, "user_id": user_id, "system": system})

    async def list_members(self, partition):
        return [{"user_id": uid, "role": role} for uid, role in self.members.items()]

    async def add_member(self, partition, user_id, role):
        self.members[user_id] = role
        self.member_changes.append(("add", user_id, role))

    async def update_role(self, partition, user_id, role):
        self.members[user_id] = role
        self.member_changes.append(("update", user_id, role))

    async def list_files(self, partition):
        return [{"file_id": file_id} for p, file_id in sorted(self.world.catalog) if p == partition]

    async def update_partition(self, partition, **fields):
        self.updates.append(fields)


class FakeUsers:
    def __init__(self, existing: User | None = None):
        self.existing = existing
        self.created: list[User] = []

    async def get_user_by_email(self, email):
        return self.existing if self.existing is not None and self.existing.email == email else None

    async def create_user(self, user):
        self.existing = user.model_copy(update={"id": CANARY_USER_ID})
        self.created.append(user)
        return self.existing


class FakeDocuments:
    def __init__(self, world):
        self.world = world

    async def file_exists_in_partition(self, file_id, partition):
        return (partition, file_id) in self.world.catalog


class FakeVectors:
    def __init__(self, world):
        self.world = world

    async def collection_exists(self, name):
        return True

    async def query_ids_by_filter(self, collection, filters):
        key = (filters["partition"], filters["file_id"])
        return ["1", "2"] if key in self.world.vectors else []


def _settings(tmp_path, **canary) -> Settings:
    base = Settings()
    return base.model_copy(
        update={
            "paths": base.paths.model_copy(update={"data_dir": str(tmp_path)}),
            "canary": CanaryConfig(
                enabled=True, **{"index_timeout_seconds": 10, "request_timeout_seconds": 5, **canary}
            ),
            "partitions": {},
        }
    )


class FakeJobs:
    def __init__(self, job):
        self.job = job

    async def get_job(self, task_id):
        return self.job


def _canary(
    tmp_path, *, world=None, indexing=None, retrieval=None, partitions=None, users=None, settings=None, jobs=None
):
    world = world or World()
    parts = SimpleNamespace(
        world=world,
        indexing=indexing or FakeIndexing(world),
        retrieval=retrieval or FakeRetrieval(world),
        partitions=partitions or FakePartitions(world),
        users=users or FakeUsers(),
    )
    parts.service = CanaryService(
        indexing_service=parts.indexing,
        retrieval_service=parts.retrieval,
        partition_service=parts.partitions,
        user_repo=parts.users,
        document_repo=FakeDocuments(world),
        vector_store=FakeVectors(world),
        settings=settings or _settings(tmp_path),
        job_repo=jobs,
        poll_interval=0,
        monotonic=Clock(),
        wall_clock=lambda: NOW,
    )
    return parts


def _canary_files(tmp_path) -> list[str]:
    directory = tmp_path / "canary"
    return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------


async def test_run_indexes_retrieves_and_deletes_as_its_own_user(tmp_path):
    canary = _canary(tmp_path)

    result = await canary.service.run_once()

    assert result.passed, result.reason
    assert result.failed_stage is None
    # Its own identity: no token, not an admin, no quota to exhaust.
    [user] = canary.users.created
    assert (user.email, user.is_admin, user.file_quota) == (CANARY_USER_EMAIL, False, -1)
    assert canary.partitions.created == [{"partition": CANARY_PARTITION, "user_id": CANARY_USER_ID, "system": True}]

    [added] = canary.indexing.added
    assert added["partition"] == CANARY_PARTITION
    assert added["user"] == {"id": CANARY_USER_ID}
    file_id = added["file_id"]
    assert canary_file_created_at(file_id) == int(NOW)
    # The query asks for the phrase only this run's document contains.
    [(partitions, query)] = canary.retrieval.calls
    assert partitions == [CANARY_PARTITION]
    assert query.split(": ", 1)[1] in added["text"]

    assert canary.indexing.deleted == [(file_id, CANARY_PARTITION)]
    assert canary.world.catalog == set()
    assert _canary_files(tmp_path) == []
    assert set(result.durations) == {
        CanaryStage.SETUP,
        CanaryStage.QUEUE,
        CanaryStage.INDEX,
        CanaryStage.QUERY,
        CanaryStage.CLEANUP,
    }
    assert all(seconds > 0 for seconds in result.durations.values())
    assert result.total_seconds > 0


async def test_each_run_uses_a_fresh_document(tmp_path):
    canary = _canary(tmp_path)

    await canary.service.run_once()
    await canary.service.run_once()

    first, second = canary.indexing.added
    assert first["file_id"] != second["file_id"]
    assert first["text"] != second["text"]
    # The user and partition are created once and reused.
    assert len(canary.users.created) == 1
    assert len(canary.partitions.created) == 1


async def test_a_miss_fails_the_query_stage_and_still_cleans_up(tmp_path):
    world = World()
    canary = _canary(tmp_path, world=world, retrieval=FakeRetrieval(world, hit=False))

    result = await canary.service.run_once()

    assert not result.passed
    assert result.failed_stage is CanaryStage.QUERY
    assert "none from canary-" in result.reason
    assert canary.indexing.deleted == [(canary.indexing.added[0]["file_id"], CANARY_PARTITION)]
    assert world.catalog == set()
    assert _canary_files(tmp_path) == []


async def test_a_failed_indexing_task_reports_its_error_and_cleans_up(tmp_path):
    world = World()
    # The worker records the whole traceback; the exception is its last line.
    error = (
        "Traceback (most recent call last):\n"
        '  File "milvus_store.py", line 945, in upsert\n'
        "    result = await self._async_client.insert(\n"
        "pymilvus.exceptions.MilvusException: <MilvusException: (code=1100, message=dimension 768 != 1024)>\n"
    )
    indexing = FakeIndexing(world, states=("QUEUED", "SERIALIZING", "FAILED"), error=error)
    canary = _canary(tmp_path, world=world, indexing=indexing)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.INDEX
    assert result.reason.endswith(
        "FAILED: pymilvus.exceptions.MilvusException: <MilvusException: (code=1100, message=dimension 768 != 1024)>"
    )
    assert "Traceback" not in result.reason
    assert indexing.deleted
    assert canary.retrieval.calls == []


async def test_a_task_nobody_picks_up_fails_the_queue_stage(tmp_path):
    world = World()
    indexing = FakeIndexing(world, states=("QUEUED",))
    canary = _canary(tmp_path, world=world, indexing=indexing)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.QUEUE
    assert "no worker picked it up" in result.reason
    assert CanaryStage.INDEX not in result.durations
    # The delete also cancels the task still waiting in the queue.
    assert indexing.deleted


async def test_a_task_that_never_finishes_fails_the_index_stage(tmp_path):
    world = World()
    indexing = FakeIndexing(world, states=("QUEUED", "SERIALIZING"))
    canary = _canary(tmp_path, world=world, indexing=indexing)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.INDEX
    assert "still SERIALIZING" in result.reason


async def test_a_submission_error_fails_the_queue_stage_and_still_deletes(tmp_path):
    # A submission whose outcome is unknown may have started a worker anyway.
    world = World()
    indexing = FakeIndexing(world, add_error=RuntimeError("Ray actor unavailable"))
    canary = _canary(tmp_path, world=world, indexing=indexing)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.QUEUE
    assert "Ray actor unavailable" in result.reason
    assert len(indexing.deleted) == 1
    assert _canary_files(tmp_path) == []


@pytest.mark.parametrize(
    ("keep", "expected"),
    [("keep_catalog_row", "catalog row"), ("keep_vectors", "chunk(s)")],
)
async def test_a_delete_that_leaves_data_behind_fails_cleanup(tmp_path, keep, expected):
    world = World()
    indexing = FakeIndexing(world)
    setattr(indexing, keep, True)
    canary = _canary(tmp_path, world=world, indexing=indexing)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.CLEANUP
    assert expected in result.reason


async def test_the_first_failing_stage_is_the_one_reported(tmp_path):
    # Cleanup failing after a miss must not hide the miss: it says what is broken.
    world = World()
    indexing = FakeIndexing(world)
    indexing.keep_catalog_row = True
    canary = _canary(tmp_path, world=world, indexing=indexing, retrieval=FakeRetrieval(world, hit=False))

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.QUERY
    # ...but the reason still says the document may be left behind.
    assert "cleanup also failed: catalog row" in result.reason


async def test_a_partition_someone_else_owns_is_never_touched(tmp_path):
    world = World()
    world.catalog.add((CANARY_PARTITION, "their-report"))
    partitions = FakePartitions(world, exists=True, members={7: "owner"})
    canary = _canary(tmp_path, world=world, partitions=partitions)

    result = await canary.service.run_once()

    assert result.failed_stage is CanaryStage.SETUP
    assert "owned by user(s) [7]" in result.reason
    assert canary.indexing.added == []
    assert canary.indexing.deleted == []
    assert partitions.member_changes == []
    assert world.catalog == {(CANARY_PARTITION, "their-report")}


@pytest.mark.parametrize(("membership", "change"), [({}, "add"), ({CANARY_USER_ID: "viewer"}, "update")])
async def test_a_recreated_canary_user_takes_its_partition_back(tmp_path, membership, change):
    # Deleting the canary user cascades its membership; the partition stays.
    world = World()
    partitions = FakePartitions(world, exists=True, members=membership)
    canary = _canary(tmp_path, world=world, partitions=partitions)

    result = await canary.service.run_once()

    assert result.passed, result.reason
    assert partitions.member_changes == [(change, CANARY_USER_ID, "owner")]


async def test_an_existing_canary_user_is_reused(tmp_path):
    existing = User(id=5, email=CANARY_USER_EMAIL, display_name="OpenRag canary")
    world = World()
    partitions = FakePartitions(world, exists=True, members={5: "owner"})
    canary = _canary(tmp_path, world=world, partitions=partitions, users=FakeUsers(existing))

    result = await canary.service.run_once()

    assert result.passed, result.reason
    assert canary.users.created == []
    assert canary.indexing.added[0]["user"] == {"id": 5}


async def test_setup_removes_only_leftovers_too_old_to_be_in_flight(tmp_path):
    world = World()
    stale = f"canary-{int(NOW) - 3600}-deadbeef"
    # Younger than the index timeout plus three request timeouts (10 + 3 * 5 s here).
    in_flight = f"canary-{int(NOW) - 20}-cafebabe"
    world.catalog |= {(CANARY_PARTITION, stale), (CANARY_PARTITION, in_flight), (CANARY_PARTITION, "admin-upload")}
    partitions = FakePartitions(world, exists=True, members={CANARY_USER_ID: "owner"})
    canary = _canary(
        tmp_path, world=world, partitions=partitions, users=FakeUsers(User(id=CANARY_USER_ID, email=CANARY_USER_EMAIL))
    )
    documents = tmp_path / "canary"
    documents.mkdir()
    (documents / f"{stale}.txt").write_text("old", encoding="utf-8")
    os.utime(documents / f"{stale}.txt", (NOW - 3600, NOW - 3600))

    result = await canary.service.run_once()

    assert result.passed, result.reason
    this_run = canary.indexing.added[0]["file_id"]
    assert canary.indexing.deleted == [(stale, CANARY_PARTITION), (this_run, CANARY_PARTITION)]
    assert world.catalog == {(CANARY_PARTITION, in_flight), (CANARY_PARTITION, "admin-upload")}
    assert _canary_files(tmp_path) == []


async def test_an_emptied_partition_follows_the_default_embedder_again(tmp_path):
    world = World()
    settings = _settings(tmp_path)
    settings.partitions[CANARY_PARTITION] = SimpleNamespace(embedder="bge-m3")
    partitions = FakePartitions(world, exists=True, members={CANARY_USER_ID: "owner"})
    canary = _canary(
        tmp_path,
        world=world,
        partitions=partitions,
        users=FakeUsers(User(id=CANARY_USER_ID, email=CANARY_USER_EMAIL)),
        settings=settings,
    )

    await canary.service.run_once()

    assert partitions.updates == [{"embedder": "default"}]


async def test_a_partition_still_holding_files_keeps_its_embedder(tmp_path):
    # Changing the embedder under existing vectors would misroute their queries.
    world = World()
    world.catalog.add((CANARY_PARTITION, "admin-upload"))
    settings = _settings(tmp_path)
    settings.partitions[CANARY_PARTITION] = SimpleNamespace(embedder="bge-m3")
    partitions = FakePartitions(world, exists=True, members={CANARY_USER_ID: "owner"})
    canary = _canary(
        tmp_path,
        world=world,
        partitions=partitions,
        users=FakeUsers(User(id=CANARY_USER_ID, email=CANARY_USER_EMAIL)),
        settings=settings,
    )

    await canary.service.run_once()

    assert partitions.updates == []


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        # The worker ran for 1.5 s of the 4 s the canary waited (submission and
        # three one-second polls on the test clock).
        (
            SimpleNamespace(
                started_at=datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC),
                completed_at=datetime(2026, 1, 1, 0, 0, 11, 500000, tzinfo=UTC),
            ),
            {CanaryStage.QUEUE: 2.5, CanaryStage.INDEX: 1.5},
        ),
        # The row does not show completion yet: the polled split stands.
        (SimpleNamespace(started_at=datetime(2026, 1, 1, tzinfo=UTC), completed_at=None), None),
    ],
)
async def test_index_time_comes_from_the_job_record_when_it_has_one(tmp_path, job, expected):
    polled = await _canary(tmp_path).service.run_once()
    result = await _canary(tmp_path, jobs=FakeJobs(job)).service.run_once()

    split = {stage: result.durations[stage] for stage in (CanaryStage.QUEUE, CanaryStage.INDEX)}
    assert split == (expected or {stage: polled.durations[stage] for stage in split})
    # Either way the two add up to the whole wait.
    assert sum(split.values()) == polled.durations[CanaryStage.QUEUE] + polled.durations[CanaryStage.INDEX]


def test_canary_file_ids_carry_their_creation_time():
    assert canary_file_created_at("canary-1800000000-0123abcd") == 1_800_000_000
    assert canary_file_created_at("canary-soon-0123abcd") is None
    assert canary_file_created_at("canary-1800000000") is None
    assert canary_file_created_at("report-1800000000-0123abcd") is None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class FakeLease:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.released = 0

    async def acquire(self):
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def release(self):
        self.released += 1


class FakeService:
    def __init__(self, results):
        self.results = list(results)
        self.runs = 0

    async def run_once(self):
        self.runs += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _scheduler(lease, service, *, sleep=asyncio.sleep, **config):
    registry = CollectorRegistry()
    scheduler = CanaryScheduler(
        service=service,
        lease=lease,
        config=CanaryConfig(enabled=True, **config),
        metrics=CanaryMetrics(registry=registry),
        wall_clock=lambda: NOW,
        sleep=sleep,
    )
    return scheduler, registry


def _sample(registry, name, **labels):
    return registry.get_sample_value(name, labels or None)


async def test_only_the_lease_holder_runs(tmp_path):
    service = FakeService([])
    scheduler, registry = _scheduler(FakeLease([False]), service)

    assert await scheduler.run_if_leader() is None
    assert service.runs == 0
    assert _sample(registry, "openrag_canary_leader") == 0


async def test_an_unreachable_lease_stands_the_replica_down(tmp_path):
    service = FakeService([])
    scheduler, registry = _scheduler(FakeLease([OSError("connection refused")]), service)

    assert await scheduler.run_if_leader() is None
    assert service.runs == 0


async def test_runs_are_recorded_as_metrics():
    failed = CanaryRunResult(
        passed=False,
        failed_stage=CanaryStage.QUERY,
        reason="miss",
        durations={CanaryStage.SETUP: 0.1, CanaryStage.QUEUE: 2.0, CanaryStage.INDEX: 3.0, CanaryStage.QUERY: 0.5},
        total_seconds=6.0,
    )
    passed = CanaryRunResult(passed=True, durations={CanaryStage.SETUP: 0.1}, total_seconds=4.0)
    scheduler, registry = _scheduler(FakeLease([True]), FakeService([failed, failed, passed]))

    await scheduler.run_if_leader()
    await scheduler.run_if_leader()
    assert _sample(registry, "openrag_canary_leader") == 1
    assert _sample(registry, "openrag_canary_consecutive_failures") == 2
    assert _sample(registry, "openrag_canary_failures_total", stage="query") == 2
    assert _sample(registry, "openrag_canary_runs_total", outcome="failure") == 2
    assert _sample(registry, "openrag_canary_last_run_timestamp_seconds") == NOW
    assert _sample(registry, "openrag_canary_last_success_timestamp_seconds") == 0
    assert _sample(registry, "openrag_canary_stage_duration_seconds", stage="index") == 3.0

    await scheduler.run_if_leader()
    assert _sample(registry, "openrag_canary_consecutive_failures") == 0
    assert _sample(registry, "openrag_canary_last_success_timestamp_seconds") == NOW
    assert _sample(registry, "openrag_canary_runs_total", outcome="success") == 1
    # A stage the last run did not reach reads 0, not an older run's value.
    assert _sample(registry, "openrag_canary_stage_duration_seconds", stage="index") == 0
    assert _sample(registry, "openrag_canary_stage_duration_seconds", stage="total") == 4.0


async def test_a_crashed_run_still_counts_as_a_failure():
    scheduler, registry = _scheduler(FakeLease([True]), FakeService([RuntimeError("bug")]))

    result = await scheduler.run_if_leader()

    assert result is not None and not result.passed
    assert _sample(registry, "openrag_canary_consecutive_failures") == 1
    assert _sample(registry, "openrag_canary_runs_total", outcome="failure") == 1


async def test_losing_the_lease_clears_the_failure_streak():
    # A former runner must not keep the alert firing for runs it no longer makes.
    failed = CanaryRunResult(passed=False, failed_stage=CanaryStage.INDEX, reason="x")
    scheduler, registry = _scheduler(FakeLease([True, True, False]), FakeService([failed, failed]))

    await scheduler.run_if_leader()
    await scheduler.run_if_leader()
    assert _sample(registry, "openrag_canary_consecutive_failures") == 2

    await scheduler.run_if_leader()
    assert _sample(registry, "openrag_canary_consecutive_failures") == 0
    assert _sample(registry, "openrag_canary_leader") == 0
    # The timestamp survives: max() across replicas is the latest run anywhere.
    assert _sample(registry, "openrag_canary_last_run_timestamp_seconds") == NOW


async def test_the_loop_runs_on_its_cadence_and_stops_cleanly():
    passed = CanaryRunResult(passed=True)
    service = FakeService([passed] * 50)
    lease = FakeLease([True])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(0)

    scheduler, registry = _scheduler(lease, service, sleep=fake_sleep, interval_seconds=60, initial_delay_seconds=0)
    scheduler.start()
    while service.runs < 3:
        await asyncio.sleep(0)
    await scheduler.stop()

    assert sleeps[0] == 0  # the initial delay
    # Start-to-start cadence: each pause is the interval minus the run's own time.
    assert all(0 < seconds <= 60 for seconds in sleeps[1:])
    assert lease.released == 1
    assert _sample(registry, "openrag_canary_leader") == 0
