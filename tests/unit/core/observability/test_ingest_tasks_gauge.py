"""S3-2 Phase 4 — the backlog gauge, sampled at scrape time.

``openrag_ingest_tasks`` is the one Tier-1 metric that is *read* rather than
counted. The authoritative number lives in the TaskStateManager actor, and a
gauge incremented on each transition would drift from it on every actor restart,
cancellation or fenced task.

Reading it during a scrape buys accuracy at the cost of a dependency, so what
these tests pin is the failure behaviour: a scrape must never fail, never block,
and never report a stale backlog as the current one.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from core.observability import monitoring
from core.observability.metric_specs import INGEST_TASK_STATE_VALUES


def _samples() -> dict[str, float]:
    return {
        sample.labels["state"]: sample.value
        for metric in monitoring.INGEST_TASKS.collect()
        for sample in metric.samples
    }


@pytest.fixture(autouse=True)
def _reset_gauge():
    monitoring.INGEST_TASKS.clear()
    yield
    monitoring.INGEST_TASKS.clear()


def test_every_declared_state_is_published_including_zero() -> None:
    """An idle queue and an absent metric must not look the same on a dashboard."""
    monitoring.set_ingest_task_counts({"QUEUED": 4})

    assert _samples() == {"QUEUED": 4.0, "SERIALIZING": 0.0}


def test_unknown_states_are_dropped() -> None:
    """``counts`` comes from the state machine, so an unexpected key is a bug —
    but it would still mint a Prometheus series. Dropping it makes that
    impossible by construction rather than by review."""
    monitoring.set_ingest_task_counts({"QUEUED": 1, "CHUNKING": 99, "": 5})

    assert set(_samples()) == set(INGEST_TASK_STATE_VALUES)


def test_clear_withdraws_the_series() -> None:
    """Stale values are worse than none: a panel showing last hour's backlog as
    current is actively misleading, where no data is honest."""
    monitoring.set_ingest_task_counts({"QUEUED": 7})
    monitoring.clear_ingest_task_counts()

    assert _samples() == {}


# ---------------------------------------------------------------------------
# The scrape must survive the actor being unavailable
# ---------------------------------------------------------------------------


class _Request:
    """Minimal stand-in; ``get_job_service`` is patched, so nothing reads it."""


async def _refresh(monkeypatch: pytest.MonkeyPatch, service: Any) -> None:
    import api.routers.admin.monitoring as route

    monkeypatch.setattr(route, "get_job_service", lambda _request: service)
    await route._refresh_ingest_tasks(_Request())


class _Service:
    def __init__(self, result: Any = None, exc: BaseException | None = None, delay: float = 0.0) -> None:
        self._result, self._exc, self._delay = result, exc, delay

    async def get_active_task_counts(self) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.asyncio
async def test_healthy_actor_populates_the_gauge(monkeypatch: pytest.MonkeyPatch) -> None:
    await _refresh(monkeypatch, _Service({"QUEUED": 3, "SERIALIZING": 2}))

    assert _samples() == {"QUEUED": 3.0, "SERIALIZING": 2.0}


@pytest.mark.asyncio
async def test_unreachable_actor_does_not_fail_the_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    monitoring.set_ingest_task_counts({"QUEUED": 99})

    await _refresh(monkeypatch, _Service(exc=RuntimeError("TaskStateManager unreachable")))

    assert _samples() == {}


@pytest.mark.asyncio
async def test_degraded_boot_does_not_fail_the_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get_job_service`` raises 503 when the container is absent. A degraded
    boot is exactly when the remaining metrics are worth having, so resolving it
    must not take the endpoint down with it."""
    import api.routers.admin.monitoring as route
    from fastapi import HTTPException

    def _raises(_request: Any) -> Any:
        raise HTTPException(status_code=503)

    monkeypatch.setattr(route, "get_job_service", _raises)
    await route._refresh_ingest_tasks(_Request())

    assert _samples() == {}


@pytest.mark.asyncio
async def test_hung_actor_is_bounded_by_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged actor must not hold the scrape open until Prometheus' own
    timeout, which would lose every other metric in the response too."""
    import api.routers.admin.monitoring as route

    monkeypatch.setattr(route, "_QUEUE_INFO_TIMEOUT_SECONDS", 0.05)

    started = asyncio.get_running_loop().time()
    await _refresh(monkeypatch, _Service(delay=5.0))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert _samples() == {}


@pytest.mark.asyncio
async def test_malformed_payload_is_treated_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shape change in ``get_active_task_counts`` must degrade to no data rather
    than raising inside a scrape.

    An empty mapping is no longer malformed — it is a valid "nothing in flight" —
    so the payload here is the wrong type entirely, which is what a contract
    change would actually look like."""
    await _refresh(monkeypatch, _Service("not-a-mapping"))

    assert _samples() == {}
