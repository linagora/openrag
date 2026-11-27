"""Retry with backoff, without Ray, and the Ray-aware version built on it."""

from __future__ import annotations

import asyncio

import pytest
from core.utils import retry


def _failing(times: int, error: BaseException):
    calls: list[int] = []

    async def attempt(index: int):
        calls.append(index)
        if len(calls) <= times:
            raise error
        return "done"

    return attempt, calls


async def test_a_failure_is_retried_until_it_succeeds():
    attempt, calls = _failing(2, RuntimeError("flaky"))

    assert await retry.retry_with_backoff(attempt, max_retries=2, base_delay=0) == "done"
    assert calls == [0, 1, 2]


async def test_a_cancellation_is_never_retried():
    attempt, calls = _failing(1, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await retry.retry_with_backoff(attempt, max_retries=3, base_delay=0)
    assert calls == [0]


async def test_a_failure_named_in_no_retry_is_raised_at_once():
    caused = RuntimeError("wrapped")
    caused.__cause__ = MemoryError("ceiling")
    attempt, calls = _failing(1, caused)

    with pytest.raises(RuntimeError):
        await retry.retry_with_backoff(attempt, max_retries=3, base_delay=0, no_retry=(MemoryError,))
    assert calls == [0]


async def test_the_ray_version_never_retries_a_cancelled_ray_task():
    from ray.exceptions import TaskCancelledError
    from services.workers import ray_utils

    attempt, calls = _failing(1, TaskCancelledError())

    with pytest.raises(TaskCancelledError):
        await ray_utils.retry_with_backoff(attempt, max_retries=3, base_delay=0)
    assert calls == [0]
    assert ray_utils.caused_by is retry.caused_by
