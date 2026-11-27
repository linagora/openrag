"""LocalDoclingEngine: bounded conversions in threads, retried, without Ray."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
import torch
from services.workers.parsers.docling_engine import LocalDoclingEngine
from services.workers.parsers.docling_workers import DoclingLoader, RayDoclingConversion
from services.workers.parsers.parser_dispatcher import ParserDispatcher


class _Converter:
    """Blocks every conversion until ``release`` is set; records what it saw."""

    def __init__(self, failures: int = 0):
        self.release = threading.Event()
        self.calls = 0
        self.running = 0
        self.most_running = 0
        self.grad_enabled: list[bool] = []
        self._failures = failures
        self._lock = threading.Lock()

    def convert(self, path: str):
        with self._lock:
            self.calls += 1
            call = self.calls
            self.running += 1
            self.most_running = max(self.most_running, self.running)
        self.grad_enabled.append(torch.is_grad_enabled())
        try:
            self.release.wait(timeout=10)
            if call <= self._failures:
                raise RuntimeError(f"conversion {call} failed")
            return f"result of {path}"
        finally:
            with self._lock:
                self.running -= 1


def _engine(converter: _Converter, *, slots: int = 1, timeout: float = 10, retries: int = 0) -> LocalDoclingEngine:
    config = SimpleNamespace(
        loader=SimpleNamespace(
            docling_max_tasks_per_worker=slots,
            docling_timeout=timeout,
            docling_max_task_retry=retries,
            docling_retry_base_delay=0,
        )
    )
    return LocalDoclingEngine(config, converter_factory=lambda: converter)


async def _until(condition, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never held"
        await asyncio.sleep(0.01)


async def test_conversions_never_run_above_the_slots():
    converter = _Converter()
    engine = _engine(converter, slots=2)

    tasks = [asyncio.create_task(engine.convert(f"/tmp/{i}.pdf")) for i in range(4)]
    await _until(lambda: converter.running == 2)
    await asyncio.sleep(0.1)
    assert converter.calls == 2, "a third conversion started while two held the slots"

    converter.release.set()
    assert sorted(await asyncio.gather(*tasks)) == [f"result of /tmp/{i}.pdf" for i in range(4)]
    assert converter.most_running == 2
    engine.close()


async def test_a_timed_out_conversion_keeps_its_slot_until_its_thread_ends():
    converter = _Converter()
    engine = _engine(converter, slots=1, timeout=0.2)

    with pytest.raises(TimeoutError):
        await engine.convert("/tmp/slow.pdf")

    # The first thread still converts: the next file waits for its slot.
    following = asyncio.create_task(engine.convert("/tmp/next.pdf"))
    await asyncio.sleep(0.3)
    assert converter.calls == 1

    converter.release.set()
    assert await following == "result of /tmp/next.pdf"
    assert converter.most_running == 1
    engine.close()


async def test_a_cancelled_caller_leaves_the_slot_to_its_thread():
    converter = _Converter()
    engine = _engine(converter, slots=1, timeout=0.5)

    abandoned = asyncio.create_task(engine.convert("/tmp/abandoned.pdf"))
    await _until(lambda: converter.running == 1)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    # The next file waits for the slot, and its timeout only starts once it has
    # one: waiting longer than the timeout must not fail it.
    following = asyncio.create_task(engine.convert("/tmp/next.pdf"))
    await asyncio.sleep(0.8)
    assert converter.calls == 1
    assert not following.done()

    converter.release.set()
    assert await following == "result of /tmp/next.pdf"
    engine.close()


async def test_a_failed_conversion_is_retried():
    converter = _Converter(failures=1)
    converter.release.set()
    engine = _engine(converter, retries=1)

    assert await engine.convert("/tmp/a.pdf") == "result of /tmp/a.pdf"
    assert converter.calls == 2
    engine.close()


async def test_a_conversion_failing_every_attempt_raises_its_error():
    converter = _Converter(failures=5)
    converter.release.set()
    engine = _engine(converter, retries=1)

    with pytest.raises(RuntimeError, match="conversion 2 failed"):
        await engine.convert("/tmp/a.pdf")
    assert converter.calls == 2
    engine.close()


async def test_a_conversion_runs_without_grad_in_its_own_thread():
    converter = _Converter()
    converter.release.set()
    engine = _engine(converter)

    await engine.convert("/tmp/a.pdf")

    assert torch.is_grad_enabled()
    assert converter.grad_enabled == [False]
    engine.close()


async def test_the_converter_is_built_once_off_the_loop():
    built_in: list[threading.Thread] = []
    converter = _Converter()
    converter.release.set()

    def factory():
        built_in.append(threading.current_thread())
        return converter

    engine = _engine(converter, slots=2)
    engine._converter_factory = factory

    await asyncio.gather(engine.start(), engine.convert("/tmp/a.pdf"), engine.convert("/tmp/b.pdf"))

    assert len(built_in) == 1
    assert built_in[0] is not threading.main_thread()
    engine.close()


# ── The switch ───────────────────────────────────────────────────────


def _dispatcher(pdf_engines: str) -> ParserDispatcher:
    config = SimpleNamespace(
        loader=SimpleNamespace(
            file_loaders=SimpleNamespace(pdf="DoclingLoader"),
            docling_max_tasks_per_worker=2,
            docling_timeout=60,
            docling_max_task_retry=0,
            docling_retry_base_delay=0,
        )
    )
    return ParserDispatcher(config, pdf_engines=pdf_engines)


def test_a_process_converting_locally_builds_docling_on_the_local_engine():
    loader = _dispatcher("local")._build_docling()._pool

    assert isinstance(loader, DoclingLoader)
    assert isinstance(loader._conversion, LocalDoclingEngine)
    loader._conversion.close()


def test_build_parser_dispatcher_passes_the_engine_choice_on():
    from services.workers.parsers.parser_dispatcher import build_parser_dispatcher

    config = _dispatcher("local")._config
    loader = build_parser_dispatcher(config, pdf_engines="local")._build_docling()._pool

    assert isinstance(loader._conversion, LocalDoclingEngine)
    loader._conversion.close()


def test_the_ray_pools_stay_the_default():
    loader = (
        ParserDispatcher(SimpleNamespace(loader=SimpleNamespace(file_loaders=SimpleNamespace(pdf="DoclingLoader"))))
        ._build_docling()
        ._pool
    )

    assert isinstance(loader._conversion, RayDoclingConversion)


def test_marker_has_no_local_engine_yet():
    with pytest.raises(ValueError, match="Marker has no local engine yet"):
        _dispatcher("local")._build_marker()


def test_an_unknown_engine_choice_is_refused():
    with pytest.raises(ValueError, match="Unknown pdf_engines 'gpu'"):
        _dispatcher("gpu")
