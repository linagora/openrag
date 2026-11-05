import asyncio
import threading

import pytest
from core.utils.exceptions import PipelineError
from services.workers import tabular_pipeline


class _BlockingIterator:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.read_thread_id: int | None = None
        self.close_thread_id: int | None = None

    def __iter__(self):
        return self

    def __next__(self):
        self.read_thread_id = threading.get_ident()
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("Test did not release the blocking iterator")
        return "batch"

    def close(self):
        self.close_thread_id = threading.get_ident()
        self.closed.set()


@pytest.mark.asyncio
async def test_cancelled_read_is_closed_by_its_worker_thread():
    batches = _BlockingIterator()
    reader = tabular_pipeline._AsyncBatchReader(batches)

    read_task = asyncio.create_task(reader.next())
    assert await asyncio.to_thread(batches.started.wait, 1)

    read_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read_task

    await asyncio.wait_for(reader.aclose(), timeout=0.5)
    assert not batches.closed.is_set()

    batches.release.set()
    assert await asyncio.to_thread(batches.closed.wait, 1)
    assert batches.close_thread_id == batches.read_thread_id


@pytest.mark.asyncio
async def test_abandoned_reads_remain_bounded_until_worker_finishes(monkeypatch):
    read_slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(tabular_pipeline, "_TABULAR_READ_SLOTS", read_slots)
    blocked_batches = _BlockingIterator()
    blocked_reader = tabular_pipeline._AsyncBatchReader(blocked_batches)
    blocked_task = asyncio.create_task(blocked_reader.next())
    assert await asyncio.to_thread(blocked_batches.started.wait, 1)

    blocked_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked_task
    await blocked_reader.aclose()

    excess_reader = tabular_pipeline._AsyncBatchReader(iter(["excess"]))
    with pytest.raises(PipelineError, match="Too many tabular parser reads"):
        await excess_reader.next()

    blocked_batches.release.set()
    assert await asyncio.to_thread(blocked_batches.closed.wait, 1)

    replacement_reader = tabular_pipeline._AsyncBatchReader(iter(["replacement"]))
    assert await replacement_reader.next() == "replacement"
    await replacement_reader.aclose()
