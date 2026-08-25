from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
import services.workers.ray_utils as ray_utils
from core.utils.exceptions import ServiceUnavailableError
from ray.exceptions import ActorDiedError, ActorUnavailableError
from services.workers.ray_utils import (
    call_ray_actor_method_with_timeout,
    call_ray_actor_with_timeout,
    retry_idempotent_ray_actor_method,
)


async def _raise(error: BaseException):
    raise error


@pytest.mark.parametrize(
    "error",
    [
        ActorDiedError(),
        ActorUnavailableError("actor is restarting", actor_id=None),
    ],
)
async def test_actor_failures_become_controlled_unavailability(error):
    with pytest.raises(ServiceUnavailableError) as caught:
        await call_ray_actor_with_timeout(
            future=asyncio.create_task(_raise(error)),
            timeout=1,
            task_description="get_all_states",
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "RAY_ACTOR_UNAVAILABLE"
    assert str(caught.value) == "RAY_ACTOR_UNAVAILABLE: Worker service is temporarily unavailable"
    assert caught.value.__cause__ is error


async def test_actor_failure_while_submitting_becomes_controlled_unavailability():
    error = ActorUnavailableError("actor is restarting", actor_id=None)

    def submit():
        raise error

    with pytest.raises(ServiceUnavailableError) as caught:
        await call_ray_actor_method_with_timeout(
            submit,
            timeout=1,
            task_description="get_all_states",
        )

    assert caught.value.status_code == 503
    assert caught.value.code == "RAY_ACTOR_UNAVAILABLE"
    assert caught.value.__cause__ is error


async def test_idempotent_actor_method_retries_temporary_unavailability(monkeypatch):
    unavailable = ServiceUnavailableError("temporarily unavailable", code="RAY_ACTOR_UNAVAILABLE")
    call = AsyncMock(side_effect=[unavailable, None])
    monkeypatch.setattr(ray_utils, "call_ray_actor_method_with_timeout", call)
    monkeypatch.setattr(ray_utils.asyncio, "sleep", AsyncMock())

    result = await retry_idempotent_ray_actor_method(
        lambda: object(),
        recovery_timeout=1,
        task_description="set_state(task-1)",
    )

    assert result is None
    assert call.await_count == 2


async def test_idempotent_actor_method_does_not_retry_operation_timeout(monkeypatch):
    call = AsyncMock(side_effect=TimeoutError)
    sleep = AsyncMock()
    monkeypatch.setattr(ray_utils, "call_ray_actor_method_with_timeout", call)
    monkeypatch.setattr(ray_utils.asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError):
        await retry_idempotent_ray_actor_method(
            lambda: object(),
            recovery_timeout=1,
            task_description="set_state(task-1)",
        )

    call.assert_awaited_once()
    sleep.assert_not_awaited()
