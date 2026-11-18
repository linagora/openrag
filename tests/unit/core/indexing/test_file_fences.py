"""The fence rules shared by every operation on a file (#1430), against an in-memory repository."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.indexing import file_fences as module
from core.indexing.file_fences import FileFences
from core.models.catalog import FileFence, FileFenceKind
from core.ports.file_fence_repo import FileFenceRepository
from core.utils.exceptions import ConflictError, FileFenceLostError

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

    async def acquire(self, *, partition, file_id, kind, holder_id, ttl_seconds) -> FileFence:
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


# ── Without fences ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_a_repository_every_rule_is_a_no_op() -> None:
    fences = FileFences(None)

    assert await fences.acquire_for_indexing(partition=PARTITION, file_id=FILE_ID, holder_id="t") is False
    await fences.acquire_for_delete(partition=PARTITION, file_id=FILE_ID, holder_id="d")
    await fences.acquire_for_metadata(partition=PARTITION, file_id=FILE_ID, holder_id="m")
    await fences.check_free(partition=PARTITION, file_id=FILE_ID)
    assert await fences.run_holding(partition=PARTITION, file_id=FILE_ID, holder_id="m", work=_returns(7)) == 7
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
        FileFences(repo).run_holding(partition=PARTITION, file_id=FILE_ID, holder_id="m", work=work), timeout=5
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
            FileFences(repo).run_holding(partition=PARTITION, file_id=FILE_ID, holder_id="m", work=work), timeout=5
        )

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_run_holding_passes_the_works_failure_through() -> None:
    async def work():
        raise ValueError("milvus down")

    with pytest.raises(ValueError, match="milvus down"):
        await FileFences(_Fences()).run_holding(partition=PARTITION, file_id=FILE_ID, holder_id="m", work=work)


# ── Delete ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_still_waiting_at_the_timeout_names_the_holder() -> None:
    repo = _Fences()
    repo.hold(FileFenceKind.METADATA, "patch-1")

    with pytest.raises(ConflictError) as caught:
        await FileFences(repo, wait_timeout=0).acquire_for_delete(partition=PARTITION, file_id=FILE_ID, holder_id="d")

    assert caught.value.code == "DOCUMENT_METADATA_UPDATE_IN_PROGRESS"
