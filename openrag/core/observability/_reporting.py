"""Failure reporting shared by the metric backends.

Recording a metric must never fail the work it measures — an indexing task
cannot die because a counter could not be written, and ``observe_stage_duration``
runs inside a ``finally`` block where a raise would replace the pipeline's real
exception with a metrics one.

Swallowing silently is the opposite failure, so each metric reports its first
problem. Keyed by metric name rather than by a single module-wide flag: these
run per document and per request, so an unkeyed guard would let the first
failure hide every other metric's for the life of the process.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()

_reported: set[str] = set()


def report_once(metric_name: str, exc: BaseException) -> None:
    """Warn the first time *metric_name* fails to record, then stay quiet."""
    if metric_name in _reported:
        return
    _reported.add(metric_name)
    logger.warning(f"metric {metric_name} could not be recorded; further failures of it are not logged: {exc}")


__all__ = ["report_once"]
