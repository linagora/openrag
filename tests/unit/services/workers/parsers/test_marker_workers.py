from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import sys
import threading
from types import SimpleNamespace

import pytest
from services.workers.parsers import marker_workers


def _config(marker_num_gpus: float = 0.25):
    """Build the minimal config shape consumed by Marker GPU selection."""
    return SimpleNamespace(loader=SimpleNamespace(marker_num_gpus=marker_num_gpus))


def test_marker_num_gpus_uses_ray_cluster_resources_when_cuda_is_hidden(monkeypatch):
    """Marker should request GPUs from Ray even when local CUDA is hidden."""
    monkeypatch.setattr(marker_workers.torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(marker_workers.ray, "cluster_resources", lambda: {"GPU": 1.0})

    assert marker_workers._marker_num_gpus(_config()) == 0.25


# ---------------------------------------------------------------------------
# _force_kill_executor — reclaiming a wedged Marker worker (#659)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, kill_error: Exception | None = None) -> None:
        self.kill_error = kill_error
        self.killed = False
        self.joined = False

    def kill(self) -> None:
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


class _FakeExecutor:
    def __init__(self, procs: list[_FakeProc]) -> None:
        self._processes = dict(enumerate(procs))
        self.shutdown_kwargs: dict | None = None

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdown_kwargs = {"wait": wait, "cancel_futures": cancel_futures}


class _NullLogger:
    def warning(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def debug(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


def test_force_kill_executor_kills_every_worker_then_shuts_down():
    procs = [_FakeProc(), _FakeProc(), _FakeProc()]
    executor = _FakeExecutor(procs)

    marker_workers._force_kill_executor(executor, _NullLogger())

    # Every worker is SIGKILLed (reclaims the wedged one) and joined...
    assert all(p.killed for p in procs)
    assert all(p.joined for p in procs)
    # ...and the executor is torn down without blocking on the wedged task.
    assert executor.shutdown_kwargs == {"wait": False, "cancel_futures": True}


def test_force_kill_executor_is_noop_for_none():
    # Must not raise when there is no executor yet (e.g. first init).
    marker_workers._force_kill_executor(None, _NullLogger())


def test_force_kill_executor_survives_a_kill_error():
    # One unkillable worker must not prevent killing the others or the shutdown.
    boom = _FakeProc(kill_error=OSError("no such process"))
    ok = _FakeProc()
    executor = _FakeExecutor([boom, ok])

    marker_workers._force_kill_executor(executor, _NullLogger())

    assert ok.killed
    assert executor.shutdown_kwargs == {"wait": False, "cancel_futures": True}


# ---------------------------------------------------------------------------
# MarkerWorker.setup_mp(slot, old_executor=...) — per-slot executors (#674, #723)
# ---------------------------------------------------------------------------


def _bare_marker_worker(slots: int = 1):
    """A MarkerWorker instance with __init__ skipped (no real models/pool)."""
    actor_class = marker_workers.MarkerWorker.__ray_metadata__.modified_class
    worker = actor_class.__new__(actor_class)
    worker.logger = _NullLogger()
    worker._workers = slots
    worker.executors = [None] * slots
    worker._executor_locks = [threading.Lock() for _ in range(slots)]
    worker.model_dict = {}
    worker.converter_config = {}
    worker.config = SimpleNamespace(
        loader=SimpleNamespace(marker_max_tasks_per_child=1, marker_child_timeout=1, marker_parse_memory_limit_mb=0)
    )
    return worker


def _patch_executor_factory(monkeypatch, new_executors: list):
    """Make setup_mp build the given fakes (in order) instead of real pools."""
    monkeypatch.setattr("torch.multiprocessing.get_start_method", lambda allow_none=False: "spawn")
    built_kwargs = []

    def factory(*args, **kwargs):
        built_kwargs.append(kwargs)
        return new_executors.pop(0)

    monkeypatch.setattr("concurrent.futures.ProcessPoolExecutor", factory)
    return built_kwargs


def test_setup_mp_skips_rebuild_when_slot_already_recycled(monkeypatch):
    """If another handler already recycled the slot, a second handler racing on
    the same stale executor must not force-kill the fresh one."""
    worker = _bare_marker_worker()
    stale_executor = _FakeExecutor([_FakeProc()])
    fresh_executor = _FakeExecutor([_FakeProc()])
    worker.executors[0] = fresh_executor  # already rebuilt by the "winning" handler
    built = _patch_executor_factory(monkeypatch, [])

    worker.setup_mp(0, old_executor=stale_executor)

    assert worker.executors[0] is fresh_executor  # left untouched
    assert fresh_executor.shutdown_kwargs is None  # never force-killed
    assert not built  # no executor was rebuilt


def test_setup_mp_rebuilds_when_old_executor_is_still_current(monkeypatch):
    """A timeout handler racing against nothing else must still reclaim the
    wedged worker: kill the slot's executor and build a fresh one."""
    worker = _bare_marker_worker()
    current_executor = _FakeExecutor([_FakeProc()])
    worker.executors[0] = current_executor
    new_executor = _FakeExecutor([])
    _patch_executor_factory(monkeypatch, [new_executor])

    worker.setup_mp(0, old_executor=current_executor)

    assert current_executor.shutdown_kwargs == {"wait": False, "cancel_futures": True}
    assert worker.executors[0] is new_executor


def test_setup_mp_always_rebuilds_when_old_executor_is_none(monkeypatch):
    """Explicit resets (init, MarkerPool recycle/health-check) always rebuild,
    regardless of what's currently installed."""
    worker = _bare_marker_worker()
    current_executor = _FakeExecutor([_FakeProc()])
    worker.executors[0] = current_executor
    new_executor = _FakeExecutor([])
    _patch_executor_factory(monkeypatch, [new_executor])

    worker.setup_mp(0)

    assert current_executor.shutdown_kwargs == {"wait": False, "cancel_futures": True}
    assert worker.executors[0] is new_executor


def test_setup_mp_resets_only_its_own_slot(monkeypatch):
    """Recycling one slot must not kill the child parsing in another slot of
    the same actor — that was the collateral kill of a shared executor."""
    worker = _bare_marker_worker(slots=3)
    slot_procs = [_FakeProc(), _FakeProc(), _FakeProc()]
    slot_executors = [_FakeExecutor([proc]) for proc in slot_procs]
    worker.executors = list(slot_executors)
    new_executor = _FakeExecutor([])
    built = _patch_executor_factory(monkeypatch, [new_executor])

    worker.setup_mp(1)

    assert slot_procs[1].killed
    assert worker.executors[1] is new_executor
    assert not slot_procs[0].killed and not slot_procs[2].killed
    assert worker.executors[0] is slot_executors[0] and worker.executors[2] is slot_executors[2]
    assert built == [
        {
            "max_workers": 1,
            "initializer": worker._worker_init,
            "initargs": ({}, 0),  # (model_dict, marker_parse_memory_limit_mb)
            "mp_context": built[0]["mp_context"],
            "max_tasks_per_child": 1,
        }
    ]


def test_is_pool_broken_checks_only_the_given_slot():
    worker = _bare_marker_worker(slots=2)
    healthy = _FakeExecutor([])
    broken = _FakeExecutor([])
    broken._broken = "A child process terminated abruptly"
    worker.executors = [healthy, broken]

    assert worker.is_pool_broken(0) is False
    assert worker.is_pool_broken(1) is True


class _TimedOutFuture:
    def result(self, timeout=None):
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        raise FuturesTimeoutError()


class _SubmitExecutor(_FakeExecutor):
    def __init__(self, procs, future) -> None:
        super().__init__(procs)
        self.future = future
        self.submitted = 0

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        return self.future


async def test_child_timeout_recycles_only_the_timed_out_slot(monkeypatch):
    """A wedged child (#659) is reclaimed by recycling its own slot; a parse
    running in another slot of the same actor keeps going."""
    worker = _bare_marker_worker(slots=2)
    other_proc, wedged_proc = _FakeProc(), _FakeProc()
    other_executor = _SubmitExecutor([other_proc], future=None)
    wedged_executor = _SubmitExecutor([wedged_proc], future=_TimedOutFuture())
    worker.executors = [other_executor, wedged_executor]
    new_executor = _FakeExecutor([])
    _patch_executor_factory(monkeypatch, [new_executor])

    from concurrent.futures import TimeoutError as FuturesTimeoutError

    try:
        await worker.process_pdf("f.pdf", page_range=[0, 1], slot=1)
    except FuturesTimeoutError:
        pass
    else:
        raise AssertionError("child timeout must propagate")

    assert wedged_executor.submitted == 1 and other_executor.submitted == 0
    assert wedged_proc.killed and worker.executors[1] is new_executor
    assert not other_proc.killed and worker.executors[0] is other_executor


# ---------------------------------------------------------------------------
# MarkerPool._process_chunk — don't release a slot the child still owns (#723)
# ---------------------------------------------------------------------------


def _bare_marker_pool():
    """A MarkerPool instance with __init__ skipped (no real Ray actors)."""
    pool_class = marker_workers.MarkerPool.__ray_metadata__.modified_class
    pool = pool_class.__new__(pool_class)
    pool.logger = _NullLogger()
    pool.config = SimpleNamespace(loader=SimpleNamespace(marker_max_task_retry=0, marker_retry_base_delay=0.01))
    pool._queue = asyncio.Queue()
    pool._queue.put_nowait("worker-1")
    return pool


class _RecordingActorMethod:
    def __init__(self, calls: list, name: str) -> None:
        self.calls = calls
        self.name = name

    def remote(self, *args, **kwargs):
        self.calls.append((self.name, args, kwargs))
        return f"ref-{self.name}"


def _recording_actor(calls: list):
    return SimpleNamespace(
        is_pool_broken=_RecordingActorMethod(calls, "is_pool_broken"),
        setup_mp=_RecordingActorMethod(calls, "setup_mp"),
        process_pdf=_RecordingActorMethod(calls, "process_pdf"),
    )


async def test_pool_helpers_address_the_slot_not_the_whole_actor(monkeypatch):
    """Every call MarkerPool makes on behalf of a slot must name that slot, so
    a recycle or health check can't reach another slot's executor."""
    pool = _bare_marker_pool()
    pool.config.loader.marker_timeout = 5
    calls = []
    worker = (_recording_actor(calls), 2)

    async def fake_call(future, timeout, task_description="Ray task"):
        return future

    monkeypatch.setattr(marker_workers, "call_ray_actor_with_timeout", fake_call)

    await pool._check_pool_broken(worker)
    await pool._reset_worker_pool(worker)
    await pool._run_chunk(worker, "f.pdf", [0, 1], "[p0-1]")

    assert calls == [
        ("is_pool_broken", (2,), {}),
        ("setup_mp", (2,), {}),
        ("process_pdf", ("f.pdf",), {"page_range": [0, 1], "slot": 2}),
    ]


async def test_process_chunk_returns_worker_to_queue_on_success(monkeypatch):
    pool = _bare_marker_pool()
    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", lambda worker, file_path, page_range, label: _return("ok"))

    result = await pool._process_chunk("f.pdf", None, "(all pages)")

    assert result == "ok"
    assert pool._queue.qsize() == 1
    assert pool._queue.get_nowait() == "worker-1"


async def test_process_chunk_recycles_before_releasing_on_cancellation(monkeypatch):
    """A cancelled/timed-out chunk must not free its slot until the worker's
    pool has been recycled — otherwise a still-busy worker re-enters rotation."""
    pool = _bare_marker_pool()
    reset_calls = []

    async def fake_reset(worker):
        reset_calls.append(worker)

    async def fake_run_chunk(worker, file_path, page_range, label):
        raise asyncio.CancelledError()

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", fake_reset)

    try:
        await pool._process_chunk("f.pdf", None, "(all pages)")
    except asyncio.CancelledError:
        pass

    # The slot is not returned inline...
    assert pool._queue.qsize() == 0

    # ...it only reappears after the background recycle has run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert reset_calls == ["worker-1"]
    assert pool._queue.qsize() == 1
    assert pool._queue.get_nowait() == "worker-1"


async def test_process_chunk_never_returns_worker_when_recycle_keeps_failing(monkeypatch):
    """If recycling can't be confirmed, the slot must stay dropped rather than
    hand back a worker that might still be running the previous parse."""
    pool = _bare_marker_pool()

    async def fake_run_chunk(worker, file_path, page_range, label):
        raise TimeoutError("parse timed out")

    async def failing_reset(worker):
        raise RuntimeError("actor unavailable")

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", failing_reset)

    try:
        await pool._process_chunk("f.pdf", None, "(all pages)")
    except Exception:
        pass

    for _ in range(5):
        await asyncio.sleep(0)

    assert pool._queue.qsize() == 0


async def test_process_chunk_returns_worker_without_recycling_on_ordinary_exception(monkeypatch):
    """A parse error (not a cancel/timeout) means the child already stopped on
    its own, so the slot must go back directly instead of recycling the whole
    actor's pool and killing sibling chunks."""
    pool = _bare_marker_pool()
    reset_calls = []

    async def fake_reset(worker):
        reset_calls.append(worker)

    async def fake_run_chunk(worker, file_path, page_range, label):
        raise RuntimeError("parse error")

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", fake_reset)

    try:
        await pool._process_chunk("f.pdf", None, "(all pages)")
    except RuntimeError:
        pass

    assert reset_calls == []
    assert pool._queue.qsize() == 1
    assert pool._queue.get_nowait() == "worker-1"


async def test_recycle_and_release_retries_across_cancellation(monkeypatch):
    """A cancel delivered to the reset call itself (e.g. the same delete that
    is tearing down the chunk) must be retried, not treated as a failed
    recycle that permanently drops the slot."""
    pool = _bare_marker_pool()
    pool._queue = asyncio.Queue()
    monkeypatch.setattr(marker_workers, "_RECYCLE_CANCEL_RETRY_DELAY", 0)
    attempts = []

    async def flaky_reset(worker):
        attempts.append(worker)
        if len(attempts) == 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(pool, "_reset_worker_pool", flaky_reset)

    await pool._recycle_and_release("worker-1", "(all pages)")

    assert len(attempts) == 2
    assert pool._queue.qsize() == 1
    assert pool._queue.get_nowait() == "worker-1"


async def _noop():
    return None


async def _return(value):
    return value


# ---------------------------------------------------------------------------
# _apply_parse_memory_limit — a hard ceiling on one parse (#997, audit A2)
# ---------------------------------------------------------------------------


def _child_alloc(mib: int) -> str:
    """Allocate ``mib`` MiB in this process; report what happened."""
    try:
        buf = bytearray(mib * 1024 * 1024)
        return f"allocated {len(buf) // (1024 * 1024)}"
    except MemoryError:
        return "MemoryError"


def _vmdata_mib() -> int:
    """This process's current private data size — what RLIMIT_DATA is measured against."""
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmData:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("VmData not reported")


def _child_limit_unchanged() -> bool:
    """Is a disabled limit a true no-op?

    The parametrized success case above can only show there is no ceiling *below
    its own size*, and sizing it to prove more means committing that much memory
    in a child on a shared runner. Reading the limit back settles it exactly and
    allocates nothing.
    """
    import resource

    from services.workers.parsers.marker_workers import _apply_parse_memory_limit

    before = resource.getrlimit(resource.RLIMIT_DATA)
    _apply_parse_memory_limit(0)
    return resource.getrlimit(resource.RLIMIT_DATA) == before


def _child_probe(headroom_mb: int | None, alloc_mib: int) -> str:
    from services.workers.parsers.marker_workers import _apply_parse_memory_limit

    # The ceiling covers the whole child, not just the parse's own growth, so it
    # has to sit above whatever the process already holds — a forked test child
    # inherits the runner's heap, and a real Marker child holds torch's.
    _apply_parse_memory_limit(0 if headroom_mb is None else _vmdata_mib() + headroom_mb)
    return _child_alloc(alloc_mib)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
@pytest.mark.parametrize(
    ("headroom_mb", "alloc_mib", "expected"),
    [
        (256, 1024, "MemoryError"),  # over the ceiling -> refused
        (256, 64, "allocated 64"),  # under it -> untouched
        (None, 64, "allocated 64"),  # disabled -> the same allocation is fine
    ],
)
def test_the_limit_actually_bounds_an_allocation_in_a_real_child(headroom_mb, alloc_mib, expected):
    """Run it for real in a forked child.

    Asserting that ``setrlimit`` was *called* would pass just as well with the
    wrong resource — and ``RLIMIT_AS`` is the wrong one here (it also refuses
    file-backed mappings, so Marker's weights and CUDA's device maps would fail).
    Only allocating against the live limit distinguishes them.
    """
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        assert pool.submit(_child_probe, headroom_mb, alloc_mib).result(timeout=60) == expected


def _child_mmap_probe(headroom_mb: int) -> str:
    """A file-backed mapping far over the ceiling must still be allowed."""
    import mmap
    import tempfile

    from services.workers.parsers.marker_workers import _apply_parse_memory_limit

    _apply_parse_memory_limit(_vmdata_mib() + headroom_mb)
    with tempfile.NamedTemporaryFile() as fh:
        fh.truncate(2 * 1024 * 1024 * 1024)
        try:
            with mmap.mmap(fh.fileno(), 0, prot=mmap.PROT_READ):
                return "mapped"
        except OSError as exc:
            return f"refused: {exc.errno}"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
def test_a_disabled_limit_leaves_the_rlimit_untouched():
    """0 must not lower the ceiling at all, not merely leave room for the test's
    own allocation."""
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        assert pool.submit(_child_limit_unchanged).result(timeout=60) is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_DATA only covers mmap on Linux")
def test_a_file_backed_mapping_is_not_counted_against_the_limit():
    """Mutation guard for the choice of resource: with ``RLIMIT_AS`` this returns
    ``refused: 12``, which is Marker failing to load its model weights."""
    ctx = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        assert pool.submit(_child_mmap_probe, 256).result(timeout=60) == "mapped"


def test_a_refused_setrlimit_does_not_stop_the_worker_starting(monkeypatch):
    """Best-effort: a platform that refuses the call must still yield a worker."""
    resource = pytest.importorskip("resource", reason="Unix-only; the missing case is covered below")

    def _boom(*_args, **_kwargs):
        raise OSError("not supported here")

    monkeypatch.setattr(resource, "setrlimit", _boom)
    monkeypatch.setattr(marker_workers, "logger", _NullLogger())

    marker_workers._apply_parse_memory_limit(256)  # must not raise


def test_a_missing_resource_module_does_not_stop_the_worker_starting(monkeypatch):
    """The case the import placement exists for — and it cannot import the module
    it is proving absent, which is why it is separate from the test above.

    ``resource`` is Unix-only. At module scope its absence would raise before any
    handler could run and the worker would not start at all; inside the function
    it degrades to a worker with no ceiling, which is the intended behaviour.
    """
    monkeypatch.setitem(sys.modules, "resource", None)  # import raises ImportError
    monkeypatch.setattr(marker_workers, "logger", _NullLogger())

    marker_workers._apply_parse_memory_limit(256)  # must not raise


# ---------------------------------------------------------------------------
# MemoryError is the one failure that leaves the child alive (#997 review)
# ---------------------------------------------------------------------------


async def test_process_chunk_recycles_the_slot_after_a_memory_error(monkeypatch):
    """The parse ceiling raises *in* the child instead of killing it, and freeing
    the objects need not bring ``VmData`` back below the limit. Returning that
    slot to the pool hands the next chunk a child that fails for a reason that is
    not its own, so a MemoryError must recycle like a timeout does."""
    pool = _bare_marker_pool()
    reset_calls = []

    async def fake_reset(worker):
        reset_calls.append(worker)

    async def fake_run_chunk(worker, file_path, page_range, label):
        raise MemoryError("parse ceiling")

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", fake_reset)

    with pytest.raises(MemoryError):
        await pool._process_chunk("f.pdf", None, "(all pages)")

    await asyncio.sleep(0)  # let the recycle task created in `finally` start
    await asyncio.sleep(0)
    assert reset_calls, "a MemoryError left the child alive but the slot was not recycled"


async def test_process_chunk_does_not_retry_a_memory_error(monkeypatch):
    """A chunk over the ceiling is over it again every time: retrying costs
    ~4x the parse plus backoff and ends with the same error.

    The pool is given a real retry budget on purpose — ``_bare_marker_pool``
    defaults to ``marker_max_task_retry=0``, where one attempt happens whether
    or not the fix is present and the assertion below proves nothing.
    """
    pool = _bare_marker_pool()
    pool.config.loader.marker_max_task_retry = 3
    attempts = []

    async def fake_run_chunk(worker, file_path, page_range, label):
        attempts.append(1)
        raise MemoryError("parse ceiling")

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", lambda worker: _noop())

    with pytest.raises(MemoryError):
        await pool._process_chunk("f.pdf", None, "(all pages)")

    assert len(attempts) == 1, f"MemoryError was retried {len(attempts)} times"


def test_ray_wrapped_memory_error_is_still_a_memory_error():
    """Both fixes above key off ``isinstance(exc, MemoryError)``, and the error
    crosses a Ray boundary first. ``as_instanceof_cause`` keeps the cause's type,
    so the check survives — pin it, because losing it would silently restore
    both the retry storm and the poisoned slot."""
    from ray.exceptions import RayTaskError

    try:
        raise MemoryError("boom")
    except MemoryError as exc:
        wrapped = RayTaskError("t", "traceback", exc).as_instanceof_cause()

    assert isinstance(wrapped, MemoryError)


async def test_an_ordinary_error_still_uses_the_retry_budget(monkeypatch):
    """Control for the test above: with the same retry budget, a non-ceiling
    failure is retried. Without this, a broken `no_retry` that swallowed every
    retry would look identical to the fix working."""
    pool = _bare_marker_pool()
    pool.config.loader.marker_max_task_retry = 3
    attempts = []

    async def fake_run_chunk(worker, file_path, page_range, label):
        attempts.append(1)
        raise RuntimeError("parse error")

    monkeypatch.setattr(pool, "ensure_worker_pool_healthy", lambda worker: _noop())
    monkeypatch.setattr(pool, "_run_chunk", fake_run_chunk)
    monkeypatch.setattr(pool, "_reset_worker_pool", lambda worker: _noop())

    with pytest.raises(RuntimeError):
        await pool._process_chunk("f.pdf", None, "(all pages)")

    assert len(attempts) == 4, f"expected 1 try + 3 retries, got {len(attempts)}"
