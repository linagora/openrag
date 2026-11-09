"""Optional, worker-safe progress reports from document parsers."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

_SUBSTAGE_REPORT_INTERVAL = 1.0


@dataclass
class _ParserProgressReporter:
    loop: asyncio.AbstractEventLoop
    tracker: Any
    loop_thread_id: int
    monotonic: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_update_at: float = 0.0
    _last_substage: tuple[str, str] | None = None

    def report(
        self,
        substage_id: str,
        label: str,
        progress: int | None,
        total: int | None,
    ) -> None:
        substage = (substage_id, label)
        now = self.monotonic()
        completed = progress is not None and total is not None and progress >= total
        with self._lock:
            if (
                substage == self._last_substage
                and not completed
                and now - self._last_update_at < _SUBSTAGE_REPORT_INTERVAL
            ):
                return
            self._last_substage = substage
            self._last_update_at = now

        if threading.get_ident() == self.loop_thread_id:
            _safe_set_substage(self.tracker, substage_id, label, progress, total)
            return

        try:
            self.loop.call_soon_threadsafe(
                _safe_set_substage,
                self.tracker,
                substage_id,
                label,
                progress,
                total,
            )
        except RuntimeError:
            # The parse task may finish as its worker loop is shutting down.
            pass


_current_reporter: contextvars.ContextVar[_ParserProgressReporter | None] = contextvars.ContextVar(
    "current_parser_progress_reporter", default=None
)


@contextmanager
def bind_parser_progress(tracker: Any | None) -> Iterator[None]:
    """Bind a reporter for one awaited parse stage; no-op without a tracker."""
    if tracker is None:
        yield
        return
    reporter = _ParserProgressReporter(asyncio.get_running_loop(), tracker, threading.get_ident())
    token = _current_reporter.set(reporter)
    try:
        yield
    finally:
        _current_reporter.reset(token)


def report_parser_substage(
    substage_id: str,
    label: str,
    *,
    progress: int | None = None,
    total: int | None = None,
) -> None:
    """Publish an observed parser milestone, or do nothing outside indexing."""
    reporter = _current_reporter.get()
    if reporter is not None:
        reporter.report(substage_id, label, progress, total)


def _safe_set_substage(
    tracker: Any,
    substage_id: str,
    label: str,
    progress: int | None,
    total: int | None,
) -> None:
    try:
        tracker.set_substage(substage_id, label, progress=progress, total=total)
    except Exception:  # noqa: BLE001 - telemetry must never affect indexing
        pass
