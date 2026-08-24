from __future__ import annotations

import asyncio

import pytest
from core.utils.exceptions import ServiceUnavailableError
from ray.exceptions import ActorDiedError, ActorUnavailableError
from services.workers.ray_utils import call_ray_actor_method_with_timeout, call_ray_actor_with_timeout


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
