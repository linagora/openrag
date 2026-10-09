from __future__ import annotations

import asyncio
import pickle
from unittest.mock import AsyncMock

import pytest
import services.workers.ray_utils as ray_utils
from core.utils.error_summary import failure_reason_from_exception
from core.utils.exceptions import ServiceUnavailableError
from ray.exceptions import ActorDiedError, ActorUnavailableError, RayTaskError
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


def _ray_task_error(cause: BaseException, function_name: str = "Actor.method") -> RayTaskError:
    """The dual-class error Ray raises in the caller when an actor method raised *cause*.

    Ray pickles the actor's exception on the way back, which keeps its type and
    message but drops ``__cause__``; the round-trip here reproduces that.
    """
    shipped = pickle.loads(pickle.dumps(cause))
    return RayTaskError(
        function_name,
        f"Traceback (most recent call last):\n{type(cause).__name__}: {cause}",
        shipped,
        proctitle=f"ray::{function_name}()",
    ).as_instanceof_cause()


async def _wrapped_failure(error: BaseException, task_description: str) -> BaseException:
    with pytest.raises(Exception) as caught:
        await call_ray_actor_with_timeout(
            future=asyncio.create_task(_raise(error)),
            timeout=1,
            task_description=task_description,
        )
    return caught.value


async def test_task_failure_names_the_remote_cause():
    remote = _ray_task_error(ValueError("bad xref table\nat offset 1234"))

    produced = await _wrapped_failure(remote, "MarkerPool PDF (f.pdf)")

    assert isinstance(produced, RuntimeError)
    assert produced.__cause__ is remote
    assert str(produced) == "MarkerPool PDF (f.pdf) failed: ValueError: bad xref table"
    assert failure_reason_from_exception(produced) == (
        "RuntimeError: MarkerPool PDF (f.pdf) failed: ValueError: bad xref table"
    )


async def test_task_failure_across_two_actors_keeps_the_root_cause():
    inner = await _wrapped_failure(
        _ray_task_error(ValueError("bad xref table"), "MarkerWorker.process_pdf"),
        "MarkerPool PDF [p10-14] (f.pdf)",
    )

    outer = await _wrapped_failure(
        _ray_task_error(inner, "MarkerPool.process_pdf"),
        "MarkerLoader PDF loading (f.pdf)",
    )

    reason = failure_reason_from_exception(outer)
    assert reason == (
        "RuntimeError: MarkerLoader PDF loading (f.pdf) failed: "
        "RuntimeError: MarkerPool PDF [p10-14] (f.pdf) failed: ValueError: bad xref table"
    )
    assert "ray::" not in reason


async def test_task_failure_with_an_empty_cause_message_names_only_the_type():
    produced = await _wrapped_failure(_ray_task_error(MemoryError()), "parse")

    assert str(produced) == "parse failed: MemoryError"


async def test_task_failure_caps_a_long_cause_but_keeps_its_end():
    long_cause = "outer wrapper context " * 40 + "failed: ValueError: root"
    produced = await _wrapped_failure(_ray_task_error(RuntimeError(long_cause)), "parse")

    cause_part = str(produced).removeprefix("parse failed: ")
    assert len(cause_part) <= 300
    assert cause_part.startswith("RuntimeError: outer wrapper context")
    assert cause_part.endswith("failed: ValueError: root")


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("cannot render")


async def test_task_failure_survives_a_cause_that_cannot_be_printed():
    remote = RayTaskError("Actor.method", "traceback", _UnprintableError()).as_instanceof_cause()

    produced = await _wrapped_failure(remote, "parse")

    assert str(produced) == "parse failed: _UnprintableError"


async def test_task_failure_without_a_cause_falls_back_to_a_generic_reason():
    remote = RayTaskError("Actor.method", "ray::Actor.method() traceback", None)

    produced = await _wrapped_failure(remote, "parse")

    assert str(produced) == "parse failed: RayTaskError"


async def test_timeout_names_the_task_and_its_bound(monkeypatch):
    monkeypatch.setattr(ray_utils.ray, "cancel", lambda *a, **k: None)

    with pytest.raises(TimeoutError) as caught:
        await call_ray_actor_with_timeout(
            future=asyncio.create_task(asyncio.sleep(10)),
            timeout=0.01,
            task_description="MarkerPool PDF (f.pdf)",
        )

    assert str(caught.value) == "MarkerPool PDF (f.pdf) timed out after 0.01s"


async def test_plain_timeout_from_the_awaited_task_reads_as_our_deadline(monkeypatch):
    monkeypatch.setattr(ray_utils.ray, "cancel", lambda *a, **k: None)
    error = TimeoutError("submit timed out")

    with pytest.raises(TimeoutError) as caught:
        await call_ray_actor_with_timeout(
            future=asyncio.create_task(_raise(error)),
            timeout=1,
            task_description="submit",
        )

    assert str(caught.value) == "submit timed out after 1s"
    assert caught.value.__cause__ is error


def _forbid_cancel(monkeypatch):
    def cancel(*_args, **_kwargs):
        raise AssertionError("a settled task must not be cancelled")

    monkeypatch.setattr(ray_utils.ray, "cancel", cancel)


async def test_timeout_raised_inside_the_actor_names_the_cause(monkeypatch):
    _forbid_cancel(monkeypatch)
    remote = _ray_task_error(TimeoutError("parse timed out"), "MarkerWorker.process_pdf")

    produced = await _wrapped_failure(remote, "MarkerPool PDF [p10-14] (f.pdf)")

    # Callers branch on the type, so a timeout stays a timeout.
    assert type(produced) is TimeoutError
    assert produced.__cause__ is remote
    reason = failure_reason_from_exception(produced)
    assert reason == "TimeoutError: MarkerPool PDF [p10-14] (f.pdf) failed: TimeoutError: parse timed out"
    assert "ray::" not in reason
    assert "\x1b" not in reason


async def test_timeout_raised_two_actors_down_keeps_the_root_cause(monkeypatch):
    _forbid_cancel(monkeypatch)
    inner = await _wrapped_failure(
        _ray_task_error(TimeoutError("parse timed out"), "MarkerWorker.process_pdf"),
        "MarkerPool PDF [p10-14] (f.pdf)",
    )

    outer = await _wrapped_failure(
        _ray_task_error(inner, "MarkerPool.process_pdf"),
        "MarkerLoader PDF loading (f.pdf)",
    )

    assert type(outer) is TimeoutError
    reason = failure_reason_from_exception(outer)
    assert reason == (
        "TimeoutError: MarkerLoader PDF loading (f.pdf) failed: "
        "TimeoutError: MarkerPool PDF [p10-14] (f.pdf) failed: TimeoutError: parse timed out"
    )
    assert "ray::" not in reason
    assert "\x1b" not in reason
