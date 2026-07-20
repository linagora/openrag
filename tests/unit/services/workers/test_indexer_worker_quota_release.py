"""Issue #664 — the worker owns the reserved quota slot after dispatch.

Admission charges one ``users.file_count`` slot *before* the job is queued,
so from dispatch onward the worker must either consume that slot (write a
catalog row) or give it back. A missed release leaks ``file_count`` upward
and permanently narrows the user's quota, so every non-success outcome gets
its own test here.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from services.workers.indexer_actor import IndexerWorker


class FakeUserRepo:
    def __init__(self, *, boom: bool = False) -> None:
        self.released: list[int] = []
        self.boom = boom

    async def release_file_slot(self, user_id: int) -> None:
        if self.boom:
            raise RuntimeError("database is down")
        self.released.append(user_id)


class FakeDocumentRepo:
    def __init__(self, *, add_result: bool = True) -> None:
        self.add_result = add_result
        self.add_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    async def add_file_to_partition(self, **kwargs: Any) -> bool:
        self.add_calls.append(kwargs)
        return self.add_result

    async def update_file_in_partition(self, **kwargs: Any) -> bool:
        self.update_calls.append(kwargs)
        return True


class _Pipeline:
    """Minimal pipeline stand-in: succeed, raise, or hang until cancelled."""

    def __init__(self, *, error: BaseException | None = None, hang: bool = False) -> None:
        self.error = error
        self.hang = hang

    async def run(self, row: dict) -> dict:
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return {**row, "stored_count": 3, "stage": "stored", "indexed_at": None}


def _tsm(*, claim: bool = True) -> MagicMock:
    tsm = MagicMock()
    tsm.set_state = MagicMock()
    tsm.set_state.remote = AsyncMock(return_value=None)
    tsm.set_failed_if_not_cancelled = MagicMock()
    tsm.set_failed_if_not_cancelled.remote = AsyncMock(return_value=True)
    # The slot is released by whoever wins this claim; ``cancel_task`` can be
    # the other contender (#664).
    tsm.claim_quota_release = MagicMock()
    tsm.claim_quota_release.remote = AsyncMock(return_value=claim)
    return tsm


def _worker(pipeline, doc_repo, user_repo, *, tsm=None) -> IndexerWorker:
    return IndexerWorker(
        pipeline=pipeline,
        task_state_manager=tsm or _tsm(),
        document_repo=doc_repo,
        topic_tag_repo=None,
        user_repo=user_repo,
    )


async def _run(worker, tmp_path, **overrides):
    path = tmp_path / "doc.txt"
    path.write_text("hello")
    kwargs: dict[str, Any] = {
        "task_id": "t1",
        "path": str(path),
        "metadata": {"file_id": "f1", "filename": "doc.txt"},
        "partition": "p1",
        "user": {"id": 42},
        "quota_reserved": True,
    }
    kwargs.update(overrides)
    return await worker.process_file(**kwargs)


@pytest.mark.asyncio
async def test_success_consumes_the_slot(tmp_path):
    """The catalog row *is* the reservation — nothing to release."""
    user_repo = FakeUserRepo()
    doc_repo = FakeDocumentRepo(add_result=True)

    result = await _run(_worker(_Pipeline(), doc_repo, user_repo), tmp_path)

    assert result["stored_count"] == 3
    assert len(doc_repo.add_calls) == 1
    assert user_repo.released == []


@pytest.mark.asyncio
async def test_a_lost_claim_leaves_the_release_to_the_canceller(tmp_path):
    """Only one party may hand the slot back.

    A task cancelled mid-flight runs this ``finally`` *and* reaches
    ``WorkerDispatcher.cancel_task``. Both would otherwise release, driving
    ``file_count`` below reality and handing the user free quota.
    """
    user_repo = FakeUserRepo()
    worker = _worker(
        _Pipeline(error=RuntimeError("boom")),
        FakeDocumentRepo(),
        user_repo,
        tsm=_tsm(claim=False),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await _run(worker, tmp_path)

    assert user_repo.released == []


@pytest.mark.asyncio
async def test_an_unreachable_state_actor_still_releases(tmp_path):
    """Arbitration is an optimisation; not leaking is the requirement.

    If the claim itself cannot be made, release anyway: an undercount is
    recoverable and self-heals on reconciliation, whereas a leak permanently
    narrows the uploader's quota.
    """
    user_repo = FakeUserRepo()
    tsm = _tsm()
    tsm.claim_quota_release.remote = AsyncMock(side_effect=RuntimeError("actor is gone"))
    worker = _worker(_Pipeline(error=RuntimeError("boom")), FakeDocumentRepo(), user_repo, tsm=tsm)

    with pytest.raises(RuntimeError, match="boom"):
        await _run(worker, tmp_path)

    assert user_repo.released == [42]


@pytest.mark.asyncio
async def test_indexing_failure_releases_the_slot(tmp_path):
    user_repo = FakeUserRepo()
    doc_repo = FakeDocumentRepo()

    with pytest.raises(RuntimeError, match="embedder exploded"):
        await _run(_worker(_Pipeline(error=RuntimeError("embedder exploded")), doc_repo, user_repo), tmp_path)

    assert user_repo.released == [42]
    assert doc_repo.add_calls == []


@pytest.mark.asyncio
async def test_cancellation_releases_the_slot(tmp_path):
    """``ray.cancel`` raises CancelledError — a BaseException.

    Regression guard: the release must not live in an ``except Exception``
    block, which would sail straight past a cancelled task and leak the slot.
    """
    user_repo = FakeUserRepo()
    worker = _worker(_Pipeline(hang=True), FakeDocumentRepo(), user_repo)

    task = asyncio.create_task(_run(worker, tmp_path))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert user_repo.released == [42]


@pytest.mark.asyncio
async def test_duplicate_at_catalog_releases_the_slot(tmp_path):
    """The 409 check is pre-dispatch, so two racers can both reach the insert.

    ``add_file_to_partition`` returns False for the loser: no file was created
    for its reservation, so the slot must go back.

    Since the rebase onto #671 the loser gets there by a different route. That
    branch added a fail-closed ``if not wrote_catalog: raise`` on the catalog
    write, so a False no longer falls through to a ``created``-gated release --
    it raises, and ``process_file``'s ``finally`` releases on the way out. The
    invariant this test exists for is unchanged and still the point: whatever
    the route, a reservation that produced no file row goes back. Both halves
    are asserted so neither can regress silently.
    """
    user_repo = FakeUserRepo()
    doc_repo = FakeDocumentRepo(add_result=False)

    with pytest.raises(RuntimeError, match="Catalog row was not written"):
        await _run(_worker(_Pipeline(), doc_repo, user_repo), tmp_path)

    assert len(doc_repo.add_calls) == 1
    assert user_repo.released == [42]


@pytest.mark.asyncio
async def test_replace_reindex_never_releases(tmp_path):
    """``put_file`` reuses an existing row, so it never reserved a slot."""
    user_repo = FakeUserRepo()
    doc_repo = FakeDocumentRepo()

    await _run(_worker(_Pipeline(), doc_repo, user_repo), tmp_path, replace=True, quota_reserved=False)

    assert doc_repo.update_calls and doc_repo.add_calls == []
    assert user_repo.released == []


@pytest.mark.asyncio
async def test_failure_without_a_reservation_releases_nothing(tmp_path):
    """A job dispatched without reserving must not decrement anyone."""
    user_repo = FakeUserRepo()

    with pytest.raises(RuntimeError):
        await _run(
            _worker(_Pipeline(error=RuntimeError("boom")), FakeDocumentRepo(), user_repo),
            tmp_path,
            quota_reserved=False,
        )

    assert user_repo.released == []


@pytest.mark.asyncio
async def test_anonymous_upload_releases_nothing(tmp_path):
    """No user id ⇒ nothing was charged ⇒ nothing to give back."""
    user_repo = FakeUserRepo()

    with pytest.raises(RuntimeError):
        await _run(
            _worker(_Pipeline(error=RuntimeError("boom")), FakeDocumentRepo(), user_repo),
            tmp_path,
            user=None,
        )

    assert user_repo.released == []


@pytest.mark.asyncio
async def test_release_failure_does_not_mask_the_indexing_error(tmp_path):
    """A broken release must not replace the real cause in the traceback."""
    user_repo = FakeUserRepo(boom=True)

    with pytest.raises(RuntimeError, match="embedder exploded"):
        await _run(
            _worker(_Pipeline(error=RuntimeError("embedder exploded")), FakeDocumentRepo(), user_repo),
            tmp_path,
        )


@pytest.mark.asyncio
async def test_state_write_failure_releases_the_slot(tmp_path):
    """The SERIALIZING write can fail — the slot must still go back.

    ``set_state`` talks to a detached Ray actor that can be unreachable
    (restart, OOM, node loss). It runs *before* the try block that owns the
    release, and ``IndexerWorkerActor.process_file`` only guards the
    catalog/registry setup, so nothing else covers this window: the request
    already handed the slot off at dispatch and will not release it.
    """
    user_repo = FakeUserRepo()
    tsm = _tsm()
    tsm.set_state.remote = AsyncMock(side_effect=RuntimeError("actor unreachable"))
    worker = IndexerWorker(
        pipeline=_Pipeline(),
        task_state_manager=tsm,
        document_repo=FakeDocumentRepo(),
        topic_tag_repo=None,
        user_repo=user_repo,
    )

    with pytest.raises(RuntimeError, match="actor unreachable"):
        await _run(worker, tmp_path)

    assert user_repo.released == [42]
