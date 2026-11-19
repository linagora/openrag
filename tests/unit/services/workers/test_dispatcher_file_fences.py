"""The dispatcher with Postgres file fences (#1430), against an in-memory fence repository."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.models.catalog import COPY_CONTENT_CLAIM_TOKEN_PREFIX, DocumentStatus, FileFence, FileFenceKind
from core.ports.file_fence_repo import FileFenceRepository
from core.utils.exceptions import (
    ConflictError,
    FileFenceLostError,
    FileFenceUnavailableError,
    ServiceUnavailableError,
)
from services.workers.dispatcher import WorkerDispatcher
from services.workers.stages.store import INDEXING_TASK_ID_METADATA_KEY

PARTITION = "tenant-a"
FILE_ID = "file-1"


class InMemoryFileFences(FileFenceRepository):
    """The port's rules, without the expiry and the terminal-job takeover."""

    def __init__(self) -> None:
        self.fences: dict[tuple[str, str], FileFence] = {}

    def hold(self, kind: FileFenceKind, holder_id: str) -> None:
        now = datetime.now(UTC)
        self.fences[(PARTITION, FILE_ID)] = FileFence(
            partition=PARTITION,
            file_id=FILE_ID,
            kind=kind,
            holder_id=holder_id,
            acquired_at=now,
            expires_at=now + timedelta(minutes=2),
        )

    def holder(self) -> FileFence | None:
        return self.fences.get((PARTITION, FILE_ID))

    async def acquire(self, *, partition, file_id, kind, holder_id, ttl_seconds) -> FileFence:
        current = self.fences.get((partition, file_id))
        if current is None or current.holder_id == holder_id:
            now = datetime.now(UTC)
            current = FileFence(
                partition=partition,
                file_id=file_id,
                kind=kind,
                holder_id=holder_id,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self.fences[(partition, file_id)] = current
        return current

    async def take_over(self, *, partition, file_id, kind, holder_id, expected_holder_id, ttl_seconds) -> bool:
        current = self.fences.get((partition, file_id))
        if current is None or current.holder_id != expected_holder_id:
            return False
        self.fences[(partition, file_id)] = current.model_copy(update={"kind": kind, "holder_id": holder_id})
        return True

    async def renew(self, *, partition, file_id, holder_id, ttl_seconds) -> bool:
        current = self.fences.get((partition, file_id))
        return current is not None and current.holder_id == holder_id

    async def renew_all(self, fences, *, ttl_seconds) -> int:
        return sum(
            [await self.renew(partition=p, file_id=f, holder_id=h, ttl_seconds=ttl_seconds) for p, f, h in fences]
        )

    async def release(self, *, partition, file_id, holder_id) -> bool:
        current = self.fences.get((partition, file_id))
        if current is None or current.holder_id != holder_id:
            return False
        del self.fences[(partition, file_id)]
        return True

    async def get(self, *, partition, file_id) -> FileFence | None:
        return self.fences.get((partition, file_id))

    async def get_live(self, *, partition, file_id) -> FileFence | None:
        return self.fences.get((partition, file_id))


def _remote_mock(return_value: Any = None) -> MagicMock:
    method = MagicMock()
    method.remote = AsyncMock(return_value=return_value)
    return method


def _task_state_manager() -> MagicMock:
    tsm = MagicMock()
    tsm.set_state = _remote_mock()
    tsm.set_failed_if_not_cancelled = _remote_mock()
    tsm.set_details = _remote_mock()
    tsm.get_content_claim_task_ids = _remote_mock(set())
    tsm.get_active_indexing_task_for_file = _remote_mock(None)
    tsm.get_matching_active_task_refs_v2 = _remote_mock({})
    tsm.set_queued_details_v2 = _remote_mock({"accepted": True, "reason": None, "existing_task_id": None})
    tsm.begin_worker_submission = _remote_mock(True)
    tsm.begin_file_delete = _remote_mock()
    tsm.renew_file_delete = _remote_mock(True)
    tsm.end_file_delete = _remote_mock()
    return tsm


def _document_repo() -> MagicMock:
    repo = MagicMock()
    repo.claim_content_sha256 = AsyncMock(return_value=None)
    repo.get_recoverable_content_sha256_claim = AsyncMock(return_value=None)
    repo.renew_content_sha256_claim = AsyncMock(return_value=True)
    repo.release_content_sha256_claim = AsyncMock()
    repo.remove_file_from_partition = AsyncMock()
    return repo


def _vector_store() -> MagicMock:
    store = MagicMock()
    store.collection_exists = AsyncMock(return_value=True)
    store.delete_by_filter = AsyncMock(return_value=0)
    return store


def _dispatcher(fences: FileFenceRepository | None, *, tsm=None, document_repo=None, pool=None, timeout=5.0):
    if pool is None:
        pool = MagicMock()
        pool.submit = _remote_mock([object()])
    workspace_repo = MagicMock()
    workspace_repo.remove_file_from_all_workspaces = AsyncMock()
    tracker = MagicMock()
    tracker.track.remote.return_value = object()
    return WorkerDispatcher(
        pool=pool,
        task_state_manager=tsm or _task_state_manager(),
        completion_tracker=tracker,
        vector_store=_vector_store(),
        document_repo=document_repo or _document_repo(),
        workspace_repo=workspace_repo,
        collection="default",
        file_fence_repo=fences,
        timeout=timeout,
    )


async def _upload(dispatcher: WorkerDispatcher, *, replace: bool = False, content_sha256: str | None = None) -> str:
    metadata: dict[str, Any] = {"file_id": FILE_ID, "filename": "report.txt"}
    if content_sha256:
        metadata["content_sha256"] = content_sha256
    return await dispatcher.dispatch_indexing(
        path="/data/report.txt",
        metadata=metadata,
        partition=PARTITION,
        user={"id": 42},
        workspace_ids=None,
        replace=replace,
    )


# ── Upload and replace ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_upload_takes_the_fence_and_hands_it_to_the_worker() -> None:
    fences = InMemoryFileFences()
    pool = MagicMock()
    pool.submit = _remote_mock([object()])

    task_id = await _upload(_dispatcher(fences, pool=pool))

    assert fences.holder().holder_id == task_id
    assert fences.holder().kind is FileFenceKind.INDEX
    assert pool.submit.remote.call_args.kwargs["fence_holder_id"] == task_id


@pytest.mark.asyncio
async def test_without_postgres_fences_the_worker_gets_no_holder() -> None:
    pool = MagicMock()
    pool.submit = _remote_mock([object()])

    await _upload(_dispatcher(None, pool=pool))

    assert "fence_holder_id" not in pool.submit.remote.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_a_file_being_indexed_is_refused_with_the_running_task(replace: bool) -> None:
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.INDEX, "task-running")
    tsm = _task_state_manager()
    repo = _document_repo()

    with pytest.raises(ConflictError) as caught:
        await _upload(_dispatcher(fences, tsm=tsm, document_repo=repo), replace=replace, content_sha256="abc")

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    assert caught.value.extra["existing_task_id"] == "task-running"
    repo.claim_content_sha256.assert_not_awaited()
    tsm.set_queued_details_v2.remote.assert_not_awaited()
    assert fences.holder().holder_id == "task-running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (FileFenceKind.DELETE, "DOCUMENT_DELETE_IN_PROGRESS"),
        (FileFenceKind.METADATA, "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"),
    ],
)
async def test_a_file_being_deleted_or_updated_is_refused(kind: FileFenceKind, code: str) -> None:
    fences = InMemoryFileFences()
    fences.hold(kind, "other-operation")

    with pytest.raises(ConflictError) as caught:
        await _upload(_dispatcher(fences), replace=True)

    assert caught.value.code == code
    assert fences.holder().holder_id == "other-operation"


def _content_conflict(dispatcher: WorkerDispatcher, tsm: MagicMock, repo: MagicMock, pool: MagicMock) -> None:
    repo.claim_content_sha256.return_value = "another-file"


def _admission_refused(dispatcher: WorkerDispatcher, tsm: MagicMock, repo: MagicMock, pool: MagicMock) -> None:
    tsm.set_queued_details_v2.remote.return_value = {
        "accepted": False,
        "reason": "outcome_backlog",
        "existing_task_id": None,
    }


def _admission_raises(dispatcher: WorkerDispatcher, tsm: MagicMock, repo: MagicMock, pool: MagicMock) -> None:
    tsm.set_queued_details_v2.remote.side_effect = RuntimeError("actor gone")


def _content_claim_lost(dispatcher: WorkerDispatcher, tsm: MagicMock, repo: MagicMock, pool: MagicMock) -> None:
    repo.renew_content_sha256_claim.return_value = False


def _submission_refused(dispatcher: WorkerDispatcher, tsm: MagicMock, repo: MagicMock, pool: MagicMock) -> None:
    tsm.begin_worker_submission.remote.return_value = False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail", "error"),
    [
        (_content_conflict, ConflictError),
        (_admission_refused, ServiceUnavailableError),
        (_admission_raises, RuntimeError),
        (_content_claim_lost, ConflictError),
        (_submission_refused, RuntimeError),
    ],
)
async def test_every_refusal_before_the_worker_starts_releases_the_fence(fail, error) -> None:
    fences = InMemoryFileFences()
    tsm = _task_state_manager()
    repo = _document_repo()
    pool = MagicMock()
    pool.submit = _remote_mock([object()])
    dispatcher = _dispatcher(fences, tsm=tsm, document_repo=repo, pool=pool)
    fail(dispatcher, tsm, repo, pool)

    with pytest.raises(error):
        await _upload(dispatcher, content_sha256="abc")

    assert fences.holder() is None
    pool.submit.remote.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_submission_whose_outcome_is_unknown_keeps_the_fence() -> None:
    """The pool may have launched the worker: it releases the fence when the task settles."""
    fences = InMemoryFileFences()
    pool = MagicMock()
    pool.submit = _remote_mock()
    pool.submit.remote.side_effect = RuntimeError("submit lost")

    with pytest.raises(RuntimeError):
        await _upload(_dispatcher(fences, pool=pool))

    assert fences.holder() is not None


@pytest.mark.asyncio
async def test_a_cancellation_during_the_queued_job_write_releases_the_fence() -> None:
    fences = InMemoryFileFences()
    repo = _document_repo()
    pool = MagicMock()
    pool.submit = _remote_mock([object()])
    dispatcher = _dispatcher(fences, document_repo=repo, pool=pool)
    writing = asyncio.Event()

    async def upsert_job(job) -> None:
        if job.status is DocumentStatus.QUEUED:
            writing.set()
            await asyncio.Event().wait()

    dispatcher._job_repo = MagicMock()
    dispatcher._job_repo.upsert_job = AsyncMock(side_effect=upsert_job)
    upload = asyncio.ensure_future(_upload(dispatcher, content_sha256="abc"))
    await asyncio.wait_for(writing.wait(), timeout=5)
    upload.cancel()

    with pytest.raises(asyncio.CancelledError):
        await upload

    assert fences.holder() is None
    repo.release_content_sha256_claim.assert_awaited_once()
    pool.submit.remote.assert_not_awaited()


# ── Delete ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_holds_the_fence_while_it_runs_and_releases_it() -> None:
    fences = InMemoryFileFences()
    tsm = _task_state_manager()
    seen: list[FileFence | None] = []
    tsm.get_matching_active_task_refs_v2.remote.side_effect = lambda **_: seen.append(fences.holder()) or {}

    await _dispatcher(fences, tsm=tsm).delete_file(FILE_ID, PARTITION)

    assert seen[0].kind is FileFenceKind.DELETE
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_delete_takes_the_fence_over_from_indexing() -> None:
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.INDEX, "task-running")
    tsm = _task_state_manager()
    seen: list[FileFence | None] = []
    tsm.get_matching_active_task_refs_v2.remote.side_effect = lambda **_: seen.append(fences.holder()) or {}

    await _dispatcher(fences, tsm=tsm).delete_file(FILE_ID, PARTITION)

    assert seen[0].kind is FileFenceKind.DELETE
    assert seen[0].holder_id != "task-running"
    assert fences.holder() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [FileFenceKind.DELETE, FileFenceKind.METADATA])
async def test_a_delete_waits_for_a_delete_or_metadata_update_to_end(kind: FileFenceKind) -> None:
    fences = InMemoryFileFences()
    fences.hold(kind, "other-operation")
    repo = _document_repo()

    async def finish_other_operation() -> None:
        await asyncio.sleep(0.2)
        await fences.release(partition=PARTITION, file_id=FILE_ID, holder_id="other-operation")

    other = asyncio.create_task(finish_other_operation())
    await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)
    await other

    repo.remove_file_from_partition.assert_awaited_once()
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_delete_still_blocked_at_the_timeout_is_refused_and_changes_nothing() -> None:
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.DELETE, "other-delete")
    tsm = _task_state_manager()
    repo = _document_repo()

    with pytest.raises(ConflictError) as caught:
        await _dispatcher(fences, tsm=tsm, document_repo=repo, timeout=0.2).delete_file(FILE_ID, PARTITION)

    assert caught.value.code == "DOCUMENT_DELETE_IN_PROGRESS"
    tsm.end_file_delete.remote.assert_awaited_once()
    repo.remove_file_from_partition.assert_not_awaited()
    assert fences.holder().holder_id == "other-delete"


@pytest.mark.asyncio
async def test_a_failed_delete_releases_the_fence() -> None:
    fences = InMemoryFileFences()
    repo = _document_repo()
    repo.remove_file_from_partition.side_effect = RuntimeError("catalog down")

    with pytest.raises(RuntimeError):
        await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)

    assert fences.holder() is None


# ── A failure after the worker was submitted ─────────────────────────


def _submitted_then_failing(fences: InMemoryFileFences) -> WorkerDispatcher:
    dispatcher = _dispatcher(fences)
    dispatcher._track_completion = MagicMock(side_effect=RuntimeError("tracker gone"))
    dispatcher._cleanup_submitted_vectors = AsyncMock()
    dispatcher._record_finished_at = AsyncMock()
    dispatcher._mark_submit_failed = AsyncMock()
    return dispatcher


@pytest.mark.asyncio
async def test_a_worker_that_could_not_be_stopped_keeps_the_fence() -> None:
    """It may still commit; the pool releases the fence once it settles."""
    fences = InMemoryFileFences()
    dispatcher = _submitted_then_failing(fences)
    dispatcher._cancel_submitted_task = AsyncMock(side_effect=TimeoutError("still running"))

    with pytest.raises(TimeoutError):
        await _upload(dispatcher)

    assert fences.holder() is not None


@pytest.mark.asyncio
async def test_a_worker_that_finished_on_its_own_keeps_the_fence_for_the_pool() -> None:
    fences = InMemoryFileFences()
    dispatcher = _submitted_then_failing(fences)
    dispatcher._cancel_submitted_task = AsyncMock(return_value=False)

    with pytest.raises(RuntimeError, match="tracker gone"):
        await _upload(dispatcher)

    assert fences.holder() is not None


@pytest.mark.asyncio
async def test_a_worker_cancelled_and_settled_releases_the_fence() -> None:
    fences = InMemoryFileFences()
    dispatcher = _submitted_then_failing(fences)
    dispatcher._cancel_submitted_task = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="tracker gone"):
        await _upload(dispatcher)

    assert fences.holder() is None


# ── Delete failures ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_that_fails_before_starting_leaves_the_upload_its_fence() -> None:
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.INDEX, "task-running")
    tsm = _task_state_manager()
    tsm.begin_file_delete.remote.side_effect = RuntimeError("task state unavailable")

    with pytest.raises(RuntimeError, match="task state unavailable"):
        await _dispatcher(fences, tsm=tsm).delete_file(FILE_ID, PARTITION)

    assert fences.holder().holder_id == "task-running"


@pytest.mark.asyncio
async def test_a_delete_that_fails_after_taking_the_fence_over_still_costs_the_upload_its_fence() -> None:
    """The upload's commit is then refused; the client retries the delete."""
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.INDEX, "task-running")
    repo = _document_repo()
    repo.remove_file_from_partition.side_effect = RuntimeError("catalog down")

    with pytest.raises(RuntimeError, match="catalog down"):
        await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)

    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_delete_survives_a_failed_fence_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.indexing.file_fences as fences_module

    monkeypatch.setattr(fences_module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.01)
    fences = InMemoryFileFences()
    renewals = 0

    async def renew_once_failing(**kwargs) -> bool:
        nonlocal renewals
        renewals += 1
        if renewals == 1:
            raise ConnectionError("postgres blip")
        return await InMemoryFileFences.renew(fences, **kwargs)

    fences.renew = renew_once_failing
    repo = _document_repo()

    async def slow_remove(**_kwargs) -> None:
        await asyncio.sleep(0.1)

    repo.remove_file_from_partition.side_effect = slow_remove

    await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)

    assert renewals >= 2
    repo.remove_file_from_partition.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_delete_whose_fence_was_lost_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.indexing.file_fences as fences_module

    monkeypatch.setattr(fences_module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.01)
    fences = InMemoryFileFences()
    fences.renew = AsyncMock(return_value=False)
    repo = _document_repo()

    async def slow_remove(**_kwargs) -> None:
        await asyncio.sleep(1)

    repo.remove_file_from_partition.side_effect = slow_remove

    with pytest.raises(FileFenceLostError):
        await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)


def _short_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0.4 s Postgres lease renewed every 0.1 s: without a renewal the delete must stop by 0.3 s."""
    import core.indexing.file_fences as fences_module

    monkeypatch.setattr(fences_module, "FILE_FENCE_TTL_SECONDS", 0.4)
    monkeypatch.setattr(fences_module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.1)


@pytest.mark.asyncio
async def test_a_delete_that_cannot_renew_stops_before_its_lease_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    _short_lease(monkeypatch)
    fences = InMemoryFileFences()
    fences.renew = AsyncMock(side_effect=ConnectionError("postgres unreachable"))
    repo = _document_repo()

    async def slow_remove(**_kwargs) -> None:
        await asyncio.sleep(5)

    repo.remove_file_from_partition.side_effect = slow_remove
    started = asyncio.get_running_loop().time()

    # A 503: the database didn't answer, nothing else took the file.
    with pytest.raises(FileFenceUnavailableError):
        await _dispatcher(fences, document_repo=repo).delete_file(FILE_ID, PARTITION)

    assert asyncio.get_running_loop().time() - started < 0.4
    assert fences.renew.await_count >= 2


@pytest.mark.asyncio
async def test_a_slow_task_state_renewal_does_not_hold_up_the_postgres_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.dispatcher as module

    _short_lease(monkeypatch)
    monkeypatch.setattr(module, "_FILE_DELETE_FENCE_RENEW_INTERVAL_SECONDS", 0.01)
    fences = InMemoryFileFences()
    renewals = 0

    async def renew_once_failing(**kwargs) -> bool:
        nonlocal renewals
        renewals += 1
        if renewals == 1:
            raise ConnectionError("postgres blip")
        return await InMemoryFileFences.renew(fences, **kwargs)

    fences.renew = renew_once_failing
    tsm = _task_state_manager()

    async def slow_task_state_renewal(**_kwargs) -> bool:
        await asyncio.sleep(1)
        return True

    tsm.renew_file_delete.remote = slow_task_state_renewal
    repo = _document_repo()

    async def slow_remove(**_kwargs) -> None:
        await asyncio.sleep(1.5)

    repo.remove_file_from_partition.side_effect = slow_remove

    # The delete outlives the 0.4 s lease, and the task state renewal takes 1 s:
    # only Postgres renewals that run on their own keep the fence.
    await _dispatcher(fences, tsm=tsm, document_repo=repo).delete_file(FILE_ID, PARTITION)

    assert renewals >= 3
    repo.remove_file_from_partition.assert_awaited_once()


# ── Metadata update ──────────────────────────────────────────────────


def _metadata_dispatcher(fences: InMemoryFileFences) -> tuple[WorkerDispatcher, MagicMock, MagicMock]:
    repo = _document_repo()
    repo.get_file_metadata = AsyncMock(return_value={"file_id": FILE_ID, "indexed_at": "2026-11-18T10:00:00+00:00"})
    repo.update_file_metadata_in_db = AsyncMock()
    store = _vector_store()
    store.query_chunks_by_filter = AsyncMock(return_value=[{"_id": 1, "text": "a", "file_id": FILE_ID}])
    store.upsert_entities = AsyncMock()
    dispatcher = _dispatcher(fences, document_repo=repo)
    dispatcher._vector_store = store
    return dispatcher, repo, store


@pytest.mark.asyncio
async def test_a_metadata_update_holds_the_metadata_fence_while_it_rewrites_the_chunks() -> None:
    fences = InMemoryFileFences()
    dispatcher, repo, store = _metadata_dispatcher(fences)
    seen: list[FileFence | None] = []
    store.upsert_entities.side_effect = lambda *_a, **_k: seen.append(fences.holder())

    await dispatcher.update_file_metadata(FILE_ID, {"title": "new"}, PARTITION, None)

    assert seen[0].kind is FileFenceKind.METADATA
    repo.update_file_metadata_in_db.assert_awaited_once()
    assert fences.holder() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (FileFenceKind.INDEX, "DOCUMENT_INDEXING_IN_PROGRESS"),
        (FileFenceKind.DELETE, "DOCUMENT_DELETE_IN_PROGRESS"),
        (FileFenceKind.METADATA, "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"),
    ],
)
async def test_a_metadata_update_of_a_busy_file_is_refused_and_writes_nothing(kind: FileFenceKind, code: str) -> None:
    fences = InMemoryFileFences()
    fences.hold(kind, "other")
    dispatcher, repo, store = _metadata_dispatcher(fences)

    with pytest.raises(ConflictError) as caught:
        await dispatcher.update_file_metadata(FILE_ID, {"title": "new"}, PARTITION, None)

    assert caught.value.code == code
    store.upsert_entities.assert_not_awaited()
    repo.update_file_metadata_in_db.assert_not_awaited()
    assert fences.holder().holder_id == "other"


@pytest.mark.asyncio
async def test_a_failed_metadata_update_releases_the_fence() -> None:
    fences = InMemoryFileFences()
    dispatcher, _, store = _metadata_dispatcher(fences)
    store.upsert_entities.side_effect = RuntimeError("milvus down")

    with pytest.raises(RuntimeError, match="milvus down"):
        await dispatcher.update_file_metadata(FILE_ID, {"title": "new"}, PARTITION, None)

    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_metadata_update_of_a_missing_file_takes_no_fence() -> None:
    fences = InMemoryFileFences()
    dispatcher, repo, store = _metadata_dispatcher(fences)
    repo.get_file_metadata.return_value = None

    await dispatcher.update_file_metadata(FILE_ID, {"title": "new"}, PARTITION, None)

    assert fences.fences == {}
    store.query_chunks_by_filter.assert_not_awaited()


# ── Copy ─────────────────────────────────────────────────────────────

SOURCE_ROW = {"file_id": "source", "partition": PARTITION, "indexed_at": "2026-11-18T10:00:00+00:00"}


def _copy_dispatcher(fences: InMemoryFileFences) -> tuple[WorkerDispatcher, MagicMock, MagicMock]:
    repo = _document_repo()
    repo.get_file_metadata = AsyncMock(return_value=dict(SOURCE_ROW))
    repo.get_indexation_config = AsyncMock(return_value=None)
    repo.add_file_to_partition = AsyncMock()
    store = _vector_store()
    store.query_chunks_by_filter = AsyncMock(return_value=[{"_id": 1, "text": "a", "file_id": "source"}])
    store.insert_entities = AsyncMock()
    dispatcher = _dispatcher(fences, document_repo=repo)
    dispatcher._vector_store = store
    return dispatcher, repo, store


async def _copy(dispatcher: WorkerDispatcher) -> None:
    await dispatcher.copy_file("source", {"file_id": FILE_ID, "partition": PARTITION}, PARTITION, None)


@pytest.mark.asyncio
async def test_a_copy_writes_its_target_under_the_targets_index_fence() -> None:
    fences = InMemoryFileFences()
    dispatcher, repo, _ = _copy_dispatcher(fences)
    seen: list[FileFence | None] = []
    repo.add_file_to_partition.side_effect = lambda **_: seen.append(fences.holder())

    await _copy(dispatcher)

    holder = seen[0]
    assert holder.kind is FileFenceKind.INDEX
    assert repo.add_file_to_partition.await_args.kwargs["fence_holder_id"] == holder.holder_id
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_long_copy_renews_its_target_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.indexing.file_fences as fences_module

    monkeypatch.setattr(fences_module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.01)
    fences = InMemoryFileFences()
    renewed: list[str] = []

    async def counting_renew(**kwargs) -> bool:
        renewed.append(kwargs["file_id"])
        return await InMemoryFileFences.renew(fences, **kwargs)

    fences.renew = counting_renew
    dispatcher, _, store = _copy_dispatcher(fences)

    async def slow_insert(*_args, **_kwargs) -> None:
        await asyncio.sleep(0.1)

    store.insert_entities.side_effect = slow_insert

    await _copy(dispatcher)

    assert renewed
    assert set(renewed) == {FILE_ID}
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_copy_that_loses_its_target_fence_writes_no_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.indexing.file_fences as fences_module

    monkeypatch.setattr(fences_module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.01)
    fences = InMemoryFileFences()
    fences.renew = AsyncMock(return_value=False)
    dispatcher, repo, store = _copy_dispatcher(fences)

    async def slow_insert(*_args, **_kwargs) -> None:
        await asyncio.sleep(1)

    store.insert_entities.side_effect = slow_insert

    with pytest.raises(FileFenceLostError):
        await _copy(dispatcher)

    repo.add_file_to_partition.assert_not_awaited()
    # The vectors the stopped insert may have written, marked with the copy's token, are removed.
    cleanup_filter = store.delete_by_filter.await_args.args[0]
    assert cleanup_filter["file_id"] == FILE_ID
    assert cleanup_filter[INDEXING_TASK_ID_METADATA_KEY].startswith(COPY_CONTENT_CLAIM_TOKEN_PREFIX)


@pytest.mark.asyncio
async def test_a_copy_reads_the_source_record_before_its_final_check() -> None:
    fences = InMemoryFileFences()
    dispatcher, repo, _ = _copy_dispatcher(fences)
    reads: list[str] = []

    async def read_metadata(*_args) -> dict:
        reads.append("metadata")
        return dict(SOURCE_ROW)

    async def read_config(*_args) -> None:
        reads.append("indexation_config")

    repo.get_file_metadata.side_effect = read_metadata
    repo.get_indexation_config.side_effect = read_config

    await _copy(dispatcher)

    # The last read is the final check: a replace finishing after the record was read is refused.
    assert reads == ["metadata", "indexation_config", "metadata"]


@pytest.mark.asyncio
async def test_a_copy_into_a_busy_file_is_refused_before_reading_the_source() -> None:
    fences = InMemoryFileFences()
    fences.hold(FileFenceKind.INDEX, "task-running")
    dispatcher, _, store = _copy_dispatcher(fences)

    with pytest.raises(ConflictError) as caught:
        await _copy(dispatcher)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    store.query_chunks_by_filter.assert_not_awaited()
    assert fences.holder().holder_id == "task-running"


@pytest.mark.asyncio
async def test_a_copy_of_a_file_being_replaced_is_refused_and_writes_nothing() -> None:
    fences = InMemoryFileFences()
    fences.fences[(PARTITION, "source")] = FileFence(
        partition=PARTITION,
        file_id="source",
        kind=FileFenceKind.INDEX,
        holder_id="replace-task",
        acquired_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=2),
    )
    dispatcher, repo, store = _copy_dispatcher(fences)

    with pytest.raises(ConflictError) as caught:
        await _copy(dispatcher)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    store.insert_entities.assert_not_awaited()
    repo.add_file_to_partition.assert_not_awaited()
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_a_copy_whose_source_changed_during_the_read_is_refused() -> None:
    fences = InMemoryFileFences()
    dispatcher, repo, store = _copy_dispatcher(fences)
    repo.get_file_metadata.side_effect = [dict(SOURCE_ROW), {**SOURCE_ROW, "indexed_at": "2026-11-18T10:00:09+00:00"}]

    with pytest.raises(ConflictError) as caught:
        await _copy(dispatcher)

    assert caught.value.code == "DOCUMENT_CHANGED_DURING_COPY"
    store.insert_entities.assert_not_awaited()
    assert fences.holder() is None


@pytest.mark.asyncio
async def test_two_copies_of_one_source_do_not_conflict() -> None:
    fences = InMemoryFileFences()
    dispatcher, _, _ = _copy_dispatcher(fences)

    await asyncio.gather(
        dispatcher.copy_file("source", {"file_id": "copy-a", "partition": PARTITION}, PARTITION, None),
        dispatcher.copy_file("source", {"file_id": "copy-b", "partition": PARTITION}, PARTITION, None),
    )

    assert fences.fences == {}
