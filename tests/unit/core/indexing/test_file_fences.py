"""The fence rules shared by every operation on a file (#1430), against an in-memory repository."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.indexing import file_fences as module
from core.indexing.file_fences import FileFences
from core.models.catalog import FileFence, FileFenceKind
from core.ports.file_fence_repo import FileFenceRepository
from core.utils.exceptions import ConflictError, FileFenceLostError, FileFenceUnavailableError

PARTITION = "tenant-a"
FILE_ID = "file-1"


class _Fences(FileFenceRepository):
    def __init__(self) -> None:
        self.fences: dict[tuple[str, str], FileFence] = {}
        self.renew_results: list[bool | Exception] = []

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

    async def acquire(
        self, *, partition, file_id, kind, holder_id, ttl_seconds, take_over_finished_task=True
    ) -> FileFence:
        # Which jobs read as finished is the database's business: recorded only.
        self.took_over_finished_task = take_over_finished_task
        current = self.fences.get((partition, file_id))
        if current is None:
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
        if self.renew_results:
            result = self.renew_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        current = self.fences.get((partition, file_id))
        return current is not None and current.holder_id == holder_id

    async def renew_all(self, fences, *, ttl_seconds) -> int:
        raise NotImplementedError

    async def release(self, *, partition, file_id, holder_id) -> bool:
        current = self.fences.get((partition, file_id))
        if current is None or current.holder_id != holder_id:
            return False
        del self.fences[(partition, file_id)]
        return True

    async def get(self, *, partition, file_id) -> FileFence | None:
        return self.fences.get((partition, file_id))

    async def get_live(self, *, partition, file_id) -> FileFence | None:
        current = self.fences.get((partition, file_id))
        return current if current is not None and current.expires_at > datetime.now(UTC) else None


# ── Without fences ───────────────────────────────────────────────────


def test_fences_start_their_metric_series_only_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(module, "start_file_fence_series", lambda: started.append(True))

    FileFences(None)
    assert started == []

    FileFences(_Fences())
    assert started == [True]


@pytest.mark.asyncio
async def test_without_a_repository_every_rule_is_a_no_op() -> None:
    fences = FileFences(None)

    assert await fences.acquire_for_indexing(partition=PARTITION, file_id=FILE_ID, holder_id="t") is False
    await fences.acquire_for_delete(partition=PARTITION, file_id=FILE_ID, holder_id="d")
    await fences.acquire_for_metadata(partition=PARTITION, file_id=FILE_ID, holder_id="m")
    await fences.check_free(partition=PARTITION, file_id=FILE_ID)
    assert (
        await fences.run_holding(
            partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=_returns(7)
        )
        == 7
    )
    assert not fences.enabled


def _returns(value):
    async def work():
        return value

    return work


# ── Metadata update ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_metadata_update_takes_a_free_fence() -> None:
    repo = _Fences()

    await FileFences(repo).acquire_for_metadata(partition=PARTITION, file_id=FILE_ID, holder_id="m")

    assert repo.fences[(PARTITION, FILE_ID)].kind is FileFenceKind.METADATA
    # Never through a job that reads as finished: its worker may still write chunks.
    assert repo.took_over_finished_task is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (FileFenceKind.INDEX, "DOCUMENT_INDEXING_IN_PROGRESS"),
        (FileFenceKind.DELETE, "DOCUMENT_DELETE_IN_PROGRESS"),
        (FileFenceKind.METADATA, "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"),
    ],
)
async def test_a_metadata_update_of_a_busy_file_is_refused(kind: FileFenceKind, code: str) -> None:
    repo = _Fences()
    repo.hold(kind, "other")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo).acquire_for_metadata(partition=PARTITION, file_id=FILE_ID, holder_id="m")

    assert caught.value.code == code
    assert repo.fences[(PARTITION, FILE_ID)].holder_id == "other"


# ── Copy source ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_free_accepts_a_free_file_and_the_callers_own_fence() -> None:
    repo = _Fences()
    fences = FileFences(repo)
    await fences.check_free(partition=PARTITION, file_id=FILE_ID)
    repo.hold(FileFenceKind.INDEX, "copy:1")

    await fences.check_free(partition=PARTITION, file_id=FILE_ID, own_holder_id="copy:1")


@pytest.mark.asyncio
async def test_check_free_refuses_a_file_another_operation_holds() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.INDEX, "task-1")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo).check_free(partition=PARTITION, file_id=FILE_ID)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"


@pytest.mark.asyncio
async def test_check_free_ignores_a_fence_whose_lease_ended() -> None:
    # A release that failed leaves the row: it must stop refusing once its lease is over.
    repo = _Fences()
    repo.hold(FileFenceKind.INDEX, "task-gone")
    fence = repo.fences[(PARTITION, FILE_ID)]
    repo.fences[(PARTITION, FILE_ID)] = fence.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )

    await FileFences(repo).check_free(partition=PARTITION, file_id=FILE_ID)


@pytest.mark.asyncio
async def test_a_file_a_copy_is_writing_is_refused_without_a_task_to_poll() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.INDEX, "copy:abc")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo).check_free(partition=PARTITION, file_id=FILE_ID)

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    assert "existing_task_id" not in caught.value.extra


@pytest.mark.asyncio
async def test_check_unchanged_passes_when_nothing_happened() -> None:
    row = {"indexed_at": "2026-11-18T10:00:00+00:00", "title": "a"}

    await FileFences(_Fences()).check_unchanged(
        partition=PARTITION, file_id=FILE_ID, before=row, read_again=_returns(dict(row))
    )


@pytest.mark.asyncio
async def test_check_unchanged_refuses_a_source_changed_during_the_read() -> None:
    before = {"indexed_at": "2026-11-18T10:00:00+00:00"}

    with pytest.raises(ConflictError) as caught:
        await FileFences(_Fences()).check_unchanged(
            partition=PARTITION,
            file_id=FILE_ID,
            before=before,
            read_again=_returns({"indexed_at": "2026-11-18T10:00:05+00:00"}),
        )

    assert caught.value.code == "DOCUMENT_CHANGED_DURING_COPY"


@pytest.mark.asyncio
async def test_check_unchanged_refuses_a_source_an_operation_holds_now() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "patch-1")
    row = {"indexed_at": "2026-11-18T10:00:00+00:00"}

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo).check_unchanged(
            partition=PARTITION, file_id=FILE_ID, before=row, read_again=_returns(dict(row))
        )

    assert caught.value.code == "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"


# ── Running while holding ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_holding_renews_while_the_work_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0)
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")
    repo.renew_results = [RuntimeError("pg down"), True, True]

    async def work():
        while repo.renew_results:
            await asyncio.sleep(0)
        return "done"

    result = await asyncio.wait_for(
        FileFences(repo).run_holding(
            partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
        ),
        timeout=5,
    )

    assert result == "done"


@pytest.mark.asyncio
async def test_run_holding_stops_the_work_once_the_fence_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0)
    repo = _Fences()
    repo.renew_results = [False]
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(FileFenceLostError):
        await asyncio.wait_for(
            FileFences(repo).run_holding(
                partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
            ),
            timeout=5,
        )

    assert stopped.is_set()


def _short_lease(monkeypatch: pytest.MonkeyPatch) -> tuple[float, float]:
    """A 0.4 s lease renewed every 0.1 s: the work must stop by 0.3 s without a renewal."""
    monkeypatch.setattr(module, "FILE_FENCE_TTL_SECONDS", 0.4)
    monkeypatch.setattr(module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0.1)
    return 0.4, 0.1


async def _run_until_stopped(repo: _Fences) -> tuple[float, asyncio.Event]:
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    started = asyncio.get_running_loop().time()
    # A 503, not a lost fence: nothing else touched the file.
    with pytest.raises(FileFenceUnavailableError) as caught:
        await asyncio.wait_for(
            FileFences(repo).run_holding(
                partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
            ),
            timeout=5,
        )
    assert caught.value.status_code == 503
    return asyncio.get_running_loop().time() - started, stopped


@pytest.mark.asyncio
async def test_run_holding_stops_the_work_a_round_before_an_unrenewed_lease_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ttl, interval = _short_lease(monkeypatch)
    repo = _Fences()
    attempts = 0

    async def failing_renew(**_kwargs) -> bool:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("pg down")

    repo.renew = failing_renew

    elapsed, stopped = await _run_until_stopped(repo)

    assert stopped.is_set()
    # Failures are retried until the deadline, then the work stops before anyone could take the fence.
    assert attempts >= 2
    assert ttl - interval - 0.05 <= elapsed < ttl


@pytest.mark.asyncio
async def test_a_renewal_that_hangs_counts_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    ttl, _ = _short_lease(monkeypatch)
    repo = _Fences()

    async def hanging_renew(**_kwargs) -> bool:
        await asyncio.Event().wait()
        return True

    repo.renew = hanging_renew

    elapsed, stopped = await _run_until_stopped(repo)

    assert stopped.is_set()
    assert elapsed < ttl


@pytest.mark.asyncio
async def test_a_renewal_whose_cancellation_stalls_does_not_hold_the_work_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ttl, _ = _short_lease(monkeypatch)
    repo = _Fences()
    unwound = asyncio.Event()

    async def stalling_renew(**_kwargs) -> bool:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A database call whose cancellation takes longer than the lease.
            await asyncio.sleep(ttl)
            unwound.set()
            raise
        return True

    repo.renew = stalling_renew

    elapsed, stopped = await _run_until_stopped(repo)

    assert stopped.is_set()
    assert elapsed < ttl
    # The abandoned renewal still finishes its cancellation on its own.
    await asyncio.wait_for(unwound.wait(), timeout=5)


@pytest.mark.asyncio
async def test_a_successful_renewal_moves_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ttl, interval = _short_lease(monkeypatch)
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")

    async def work():
        await asyncio.sleep(ttl * 2)
        return "done"

    result = await FileFences(repo).run_holding(
        partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
    )

    assert result == "done"


@pytest.mark.asyncio
async def test_work_that_ends_in_the_round_its_fence_is_lost_reports_its_own_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both tasks yield once, then finish in the same loop iteration: the work
    # returns while the renewal finds the fence gone.
    monkeypatch.setattr(module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0)
    repo = _Fences()
    repo.renew_results = [False]

    async def work():
        await asyncio.sleep(0)
        return "committed"

    result = await FileFences(repo).run_holding(
        partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
    )

    assert result == "committed"
    assert repo.renew_results == []


@pytest.mark.asyncio
async def test_run_holding_passes_the_works_failure_through() -> None:
    async def work():
        raise ValueError("milvus down")

    with pytest.raises(ValueError, match="milvus down"):
        await FileFences(_Fences()).run_holding(
            partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
        )


# ── hold: release unless the work was stopped ───────────────────────


async def _hold(repo: _Fences, work):
    return await FileFences(repo).hold(
        partition=PARTITION, file_id=FILE_ID, holder_id="m", holder_kind=FileFenceKind.METADATA, work=work
    )


@pytest.mark.asyncio
async def test_hold_releases_the_fence_of_work_that_ended() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")

    assert await _hold(repo, _returns("done")) == "done"

    assert (PARTITION, FILE_ID) not in repo.fences


@pytest.mark.asyncio
async def test_hold_releases_the_fence_of_work_that_failed_on_its_own() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")

    async def work():
        raise ValueError("milvus down")

    with pytest.raises(ValueError, match="milvus down"):
        await _hold(repo, work)

    assert (PARTITION, FILE_ID) not in repo.fences


@pytest.mark.asyncio
async def test_hold_leaves_the_fence_of_work_stopped_by_an_unrenewed_lease_to_expire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A write the stopped work already sent may still land: releasing would let
    # another operation take the file before it does.
    _short_lease(monkeypatch)
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")

    async def failing_renew(**_kwargs) -> bool:
        raise RuntimeError("pg down")

    repo.renew = failing_renew

    async def work():
        await asyncio.Event().wait()

    with pytest.raises(FileFenceUnavailableError):
        await asyncio.wait_for(_hold(repo, work), timeout=5)

    assert repo.fences[(PARTITION, FILE_ID)].holder_id == "m"


@pytest.mark.asyncio
async def test_hold_leaves_the_fence_of_work_whose_caller_was_cancelled_to_expire() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "m")
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    holding = asyncio.ensure_future(_hold(repo, work))
    await asyncio.wait_for(started.wait(), timeout=5)
    holding.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holding

    assert repo.fences[(PARTITION, FILE_ID)].holder_id == "m"


# ── Delete ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_still_waiting_at_the_timeout_names_the_holder() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "patch-1")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo, wait_timeout=0).acquire_for_delete(partition=PARTITION, file_id=FILE_ID, holder_id="d")

    assert caught.value.code == "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"


@pytest.mark.asyncio
async def test_a_delete_that_must_not_take_over_indexing_is_refused_instead() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.INDEX, "task-1")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo).acquire_for_delete(
            partition=PARTITION, file_id=FILE_ID, holder_id="d", take_over_indexing=False
        )

    assert caught.value.code == "DOCUMENT_INDEXING_IN_PROGRESS"
    assert repo.fences[(PARTITION, FILE_ID)].holder_id == "task-1"
    # Nor through a job that already reads as finished: its worker may still write.
    assert repo.took_over_finished_task is False


# ── Metrics ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refusal_is_counted_by_operation_and_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[dict] = []
    monkeypatch.setattr(module, "record_file_fence_conflict", lambda **labels: recorded.append(labels))
    repo = _Fences()
    repo.hold(FileFenceKind.DELETE, "d")
    fences = FileFences(repo)

    with pytest.raises(ConflictError):
        await fences.acquire_for_indexing(partition=PARTITION, file_id=FILE_ID, holder_id="copy:1", operation="copy")
    with pytest.raises(ConflictError):
        await fences.acquire_for_metadata(partition=PARTITION, file_id=FILE_ID, holder_id="m")

    assert recorded == [
        {"file_operation": "copy", "holder_kind": "delete"},
        {"file_operation": "metadata", "holder_kind": "delete"},
    ]


@pytest.mark.asyncio
async def test_a_failed_renewal_is_counted_by_holder_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "FILE_FENCE_RENEW_INTERVAL_SECONDS", 0)
    recorded: list[dict] = []
    monkeypatch.setattr(module, "record_file_fence_renewal_failure", lambda **labels: recorded.append(labels))
    repo = _Fences()
    repo.renew_results = [RuntimeError("pg down"), False]

    with pytest.raises(FileFenceLostError):
        await asyncio.wait_for(
            FileFences(repo).run_holding(
                partition=PARTITION,
                file_id=FILE_ID,
                holder_id="d",
                holder_kind=FileFenceKind.DELETE,
                work=lambda: asyncio.Event().wait(),
            ),
            timeout=5,
        )

    assert recorded == [{"holder_kind": "delete"}]
