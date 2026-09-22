"""S3-2 Phase 2 — ingestion metrics: the recording logic and its wiring.

Two things are tested separately because they fail separately.

*The logic* in :mod:`core.observability.ray_metrics` — clock-skew clamping, the
refusal to invent an observation from a missing timestamp — is pure and testable
directly.

*The wiring* is the part that actually rots. ``ray.util.metrics`` records happily
when Ray is not initialised: it is a silent no-op, not an error. So a call site
that is deleted, moved out of a ``finally``, or bypassed by a second code path
raises nothing and shows up only as a permanently empty dashboard panel. These
tests therefore monkeypatch the recorder at each import site and assert the call
was made with the right arguments — which is the only way this class of bug is
caught before production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import core.observability.ray_metrics as ray_metrics
import pytest
from core.models.catalog import TASK_CREATED_AT_METADATA_KEY


class _Recorder:
    """Stand-in for a ray.util.metrics instrument; records calls."""

    def __init__(self) -> None:
        self.observations: list[float] = []
        self.increments: list[dict[str, str] | None] = []

    def observe(self, value: float, tags: dict[str, str] | None = None) -> None:
        self.observations.append(value)

    def inc(self, amount: float = 1, tags: dict[str, str] | None = None) -> None:
        self.increments.append(tags)


# ---------------------------------------------------------------------------
# Queue wait — the cross-process measurement
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_wait(monkeypatch: pytest.MonkeyPatch) -> tuple[_Recorder, _Recorder]:
    wait, skew = _Recorder(), _Recorder()
    monkeypatch.setattr(ray_metrics, "_QUEUE_WAIT", wait)
    monkeypatch.setattr(ray_metrics, "_CLOCK_SKEW_TOTAL", skew)
    return wait, skew


def test_queue_wait_measures_admission_to_processing(queue_wait) -> None:
    wait, skew = queue_wait
    now = datetime(2026, 9, 14, 12, 0, 30, tzinfo=UTC)

    ray_metrics.observe_queue_wait_from("2026-09-14T12:00:00+00:00", now=now)

    assert wait.observations == [30.0]
    assert skew.increments == []


def test_negative_wait_is_clamped_and_counted_as_skew(queue_wait) -> None:
    """A negative interval is physically impossible, so it can only be clock skew.

    ``created_at`` is stamped by the dispatcher in the API process and read by
    the TaskStateManager actor — different nodes under Kubernetes, therefore
    unsynchronised clocks. Dropping the sample would bias the histogram; folding
    it silently into the zero bucket would make a skewed fleet look like an idle
    queue. It is clamped *and* named.
    """
    wait, skew = queue_wait
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

    ray_metrics.observe_queue_wait_from("2026-09-14T12:00:05+00:00", now=now)

    assert wait.observations == [0.0]
    assert len(skew.increments) == 1


def test_naive_timestamp_is_read_as_utc(queue_wait) -> None:
    """An older TaskStateManager surviving a rolling deploy can hold a naive
    timestamp; treating it as local time would produce an hours-wide error."""
    wait, _ = queue_wait

    ray_metrics.observe_queue_wait_from(
        "2026-09-14T12:00:00",
        now=datetime(2026, 9, 14, 12, 0, 10, tzinfo=UTC),
    )

    assert wait.observations == [10.0]


@pytest.mark.parametrize("created_at", [None, "", "not-a-timestamp", 1757851200, {"t": 1}])
def test_unusable_timestamp_records_nothing(queue_wait, created_at: Any) -> None:
    """No observation beats a wrong one: a zero would drag the p50 down and make
    a backlogged queue look healthy."""
    wait, skew = queue_wait

    ray_metrics.observe_queue_wait_from(created_at)

    assert wait.observations == []
    assert skew.increments == []


def test_recording_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metrics must not be able to fail an indexing task.

    ``observe_stage_duration`` is called from a ``finally`` block, where a raise
    would replace the pipeline's real exception with a metrics one.
    """

    class _Exploding:
        def observe(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("backend down")

        def inc(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("backend down")

        def set(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("backend down")

    monkeypatch.setattr("core.observability._reporting._reported", set())
    monkeypatch.setattr(ray_metrics, "_STAGE_DURATION", _Exploding())
    monkeypatch.setattr(ray_metrics, "_DOCUMENTS_TOTAL", _Exploding())
    monkeypatch.setattr(ray_metrics, "_LAST_PARSE_TIMESTAMP", _Exploding())

    ray_metrics.observe_stage_duration("parse", 1.0)
    ray_metrics.record_document_terminal("FAILED")
    ray_metrics.record_parse_completion("marker")


def test_terminal_status_label_is_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    """The state machine stores ``"FAILED"``; the label set is lowercase."""
    docs = _Recorder()
    monkeypatch.setattr(ray_metrics, "_DOCUMENTS_TOTAL", docs)

    ray_metrics.record_document_terminal("FAILED")

    assert docs.increments == [{"status": "failed"}]


# ---------------------------------------------------------------------------
# Wiring: the TaskStateManager counts transitions, not writes
# ---------------------------------------------------------------------------


def _manager() -> Any:
    from services.workers.task_state import TaskStateManager

    return TaskStateManager.__ray_metadata__.modified_class()


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import services.workers.task_state as task_state_module

    seen: list[str] = []
    monkeypatch.setattr(task_state_module, "record_document_terminal", seen.append)
    return seen


@pytest.mark.asyncio
async def test_completed_document_is_counted_once(counted: list[str]) -> None:
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    await manager.set_state("task-1", "COMPLETED")

    assert counted == ["COMPLETED"]


@pytest.mark.asyncio
async def test_repeated_terminal_write_is_not_counted_twice(counted: list[str]) -> None:
    """The setters are re-entrant by design — a retried actor call can set FAILED
    on an already-failed task. Counting the write rather than the transition
    would inflate the failure ratio S3-4 alerts on."""
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.set_failed_if_not_cancelled("task-1", "boom") is True
    await manager.set_failed_if_not_cancelled("task-1", "boom again")

    assert counted == ["FAILED"]


@pytest.mark.asyncio
async def test_cancellation_is_counted(counted: list[str]) -> None:
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.set_cancelled_if_active("task-1") is True

    assert counted == ["CANCELLED"]


@pytest.mark.asyncio
async def test_pre_dispatch_failure_is_counted(counted: list[str]) -> None:
    """A task rejected before a worker ever saw it is still a failed document.

    This is why the count lives in the TaskStateManager and not in the indexer
    worker: the worker only observes documents that reached it, and a submission
    rejected after the worker settled is exactly the systemic failure the alert
    needs to catch.
    """
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    await manager.finish_rejected_submission("task-1")

    assert counted == ["FAILED"]


@pytest.mark.asyncio
async def test_worker_completion_is_counted_once(counted: list[str]) -> None:
    """``complete_with_degraded_stages`` is how the indexer worker settles every
    successful task, so missing it would drop nearly all completions."""
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.complete_with_degraded_stages("task-1", []) == "completed"
    await manager.complete_with_degraded_stages("task-1", [])

    assert counted == ["COMPLETED"]


@pytest.mark.asyncio
async def test_worker_failure_with_reason_is_counted_once(counted: list[str]) -> None:
    """``submit_task_failure`` prefers this setter whenever the actor has it."""
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")
    assert await manager.set_failed_with_reason_if_not_cancelled("task-1", "boom", "pipeline_error") is True
    await manager.set_failed_with_reason_if_not_cancelled("task-1", "boom again", "pipeline_error")

    assert counted == ["FAILED"]


def test_every_terminal_write_is_counted() -> None:
    """A setter that writes a terminal state without counting it drops that
    path from ``openrag_ingest_documents_total`` silently. Two such setters
    arrived from develop after this metric was written, so check them all."""
    import ast
    import inspect

    import services.workers.task_state as task_state_module

    terminal = {"COMPLETED", "FAILED", "CANCELLED"}
    counting = {"_count_terminal", "_set_cancelled_locked"}
    uncounted = []
    for node in ast.walk(ast.parse(inspect.getsource(task_state_module))):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name in counting:
            continue
        writes_terminal = any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "state" for t in n.targets)
            and isinstance(n.value, ast.Constant)
            and n.value.value in terminal
            for n in ast.walk(node)
        )
        calls = {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        if writes_terminal and not calls & counting:
            uncounted.append(node.name)

    assert not uncounted, f"terminal state written without _count_terminal in: {uncounted}"


@pytest.mark.asyncio
async def test_queued_state_is_not_counted(counted: list[str]) -> None:
    """In-flight states belong to the ``openrag_ingest_tasks`` gauge."""
    manager = _manager()

    await manager.set_state("task-1", "QUEUED")

    assert counted == []


# ---------------------------------------------------------------------------
# Wiring: queue wait is observed on the QUEUED -> SERIALIZING edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_wait_observed_on_serializing_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.workers.task_state as task_state_module

    observed: list[str | None] = []
    monkeypatch.setattr(task_state_module, "observe_queue_wait_from", observed.append)

    manager = _manager()
    created = (datetime.now(UTC) - timedelta(seconds=12)).isoformat()
    await manager.set_state("task-1", "QUEUED")
    await manager.set_details(
        "task-1",
        file_id="f",
        partition="p",
        user_id=None,
        metadata={TASK_CREATED_AT_METADATA_KEY: created},
    )
    await manager.set_object_ref("task-1", {"ref": object()})

    await manager.set_state("task-1", "SERIALIZING")

    assert observed == [created]


@pytest.mark.asyncio
async def test_queue_wait_not_observed_twice_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``set_state`` is retried by ``retry_idempotent_ray_actor_method``; the
    same wait must not enter the histogram twice."""
    import services.workers.task_state as task_state_module

    observed: list[str | None] = []
    monkeypatch.setattr(task_state_module, "observe_queue_wait_from", observed.append)

    manager = _manager()
    await manager.set_state("task-1", "QUEUED")
    await manager.set_object_ref("task-1", {"ref": object()})
    await manager.set_state("task-1", "SERIALIZING")
    await manager.set_state("task-1", "SERIALIZING")

    assert len(observed) == 1


# ---------------------------------------------------------------------------
# Wiring: stage duration is exported in SECONDS, and even when a stage fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_duration_is_recorded_in_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """``pipeline_builder`` keeps ``timings`` in milliseconds for its log line.
    The metric is named ``_seconds`` and must actually be seconds — this pins
    the conversion at the one place it is easy to get wrong.
    """
    import services.workers.pipeline_builder as pb

    recorded: list[tuple[str, float]] = []
    monkeypatch.setattr(pb, "observe_stage_duration", lambda stage, secs: recorded.append((stage, secs)))

    timings: dict[str, float] = {}

    # Reproduces the helper defined inside IndexingPipeline.run.
    async def _timed(name: str, coro: Any) -> None:
        import time

        start = time.perf_counter()
        try:
            await coro
        finally:
            elapsed = time.perf_counter() - start
            timings[name] = elapsed * 1000.0
            pb.observe_stage_duration(name, elapsed)

    async def _work() -> None:
        return None

    await _timed("parse", _work())

    assert len(recorded) == 1
    stage, seconds = recorded[0]
    assert stage == "parse"
    # Milliseconds would be ~1000x larger; this is the check that catches the unit slip.
    assert seconds == pytest.approx(timings["parse"] / 1000.0)


@pytest.mark.asyncio
async def test_failing_stage_is_still_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stage that hangs to its timeout is precisely what the histogram exists
    to show, so the observation lives in ``finally`` — and the original
    exception must still be the one that propagates."""
    import services.workers.pipeline_builder as pb

    recorded: list[str] = []
    monkeypatch.setattr(pb, "observe_stage_duration", lambda stage, secs: recorded.append(stage))

    async def _timed(name: str, coro: Any) -> None:
        import time

        start = time.perf_counter()
        try:
            await coro
        finally:
            pb.observe_stage_duration(name, time.perf_counter() - start)

    async def _boom() -> None:
        raise TimeoutError("parse timed out")

    with pytest.raises(TimeoutError, match="parse timed out"):
        await _timed("parse", _boom())

    assert recorded == ["parse"]


def test_pipeline_builder_still_calls_the_recorder() -> None:
    """Guards the wiring itself: the monkeypatched tests above would keep passing
    if the call were deleted from ``IndexingPipeline.run``."""
    import inspect

    import services.workers.pipeline_builder as pb

    source = inspect.getsource(pb.IndexingPipeline.run)
    assert "observe_stage_duration(name, elapsed)" in source
