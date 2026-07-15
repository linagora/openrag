from __future__ import annotations

from types import SimpleNamespace

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
