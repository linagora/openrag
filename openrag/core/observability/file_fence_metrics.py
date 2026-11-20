"""Metrics of the per-file fences (#1430).

Recorded on both sides of Ray, like the inference metrics: a refusal or a
renewal happens in the API process, a refused commit and a worker's renewal in
an indexing worker. Each process records through the one backend it can be
scraped on (``_dual_backend``).

Recording never fails the operation it measures: a failure is reported once
per metric and swallowed.
"""

from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple

from core.observability._dual_backend import Instrument, PrometheusInstrument, RayInstrument, use_ray_backend
from core.observability._reporting import report_once
from core.observability.metric_specs import (
    FILE_FENCE_COMMITS_REFUSED_TOTAL,
    FILE_FENCE_CONFLICTS_TOTAL,
    FILE_FENCE_HOLDER_KIND_VALUES,
    FILE_FENCE_RENEWAL_FAILURES_TOTAL,
    FILE_OPERATION_VALUES,
    MetricSpec,
)


class _Instruments(NamedTuple):
    conflicts: Instrument
    commits_refused: Instrument
    renewal_failures: Instrument


@lru_cache(maxsize=1)
def _instruments() -> _Instruments:
    if use_ray_backend():
        from ray.util import metrics as ray_metrics

        def counter(spec: MetricSpec) -> Instrument:
            return RayInstrument(ray_metrics.Counter(spec.name, description=spec.description, tag_keys=spec.labels))
    else:
        import prometheus_client

        def counter(spec: MetricSpec) -> Instrument:
            return PrometheusInstrument(prometheus_client.Counter(spec.name, spec.description, list(spec.labels)))

    return _Instruments(
        conflicts=counter(FILE_FENCE_CONFLICTS_TOTAL),
        commits_refused=counter(FILE_FENCE_COMMITS_REFUSED_TOTAL),
        renewal_failures=counter(FILE_FENCE_RENEWAL_FAILURES_TOTAL),
    )


def start_file_fence_series() -> None:
    """Create every fence series of this process at 0, before its first event.

    ``rate()``/``increase()`` count the change between samples, so a series
    first scraped at 1 loses that event; these events are rare, and each Ray
    worker has its own series, so most would be lost. The label values are
    closed sets. Called where a process starts using fences; calling it again
    changes nothing.
    """
    try:
        instruments = _instruments()
        for file_operation in FILE_OPERATION_VALUES:
            for holder_kind in FILE_FENCE_HOLDER_KIND_VALUES:
                instruments.conflicts.start_at_zero({"file_operation": file_operation, "holder_kind": holder_kind})
        instruments.commits_refused.start_at_zero({})
        for holder_kind in FILE_FENCE_HOLDER_KIND_VALUES:
            instruments.renewal_failures.start_at_zero({"holder_kind": holder_kind})
    except Exception as exc:  # noqa: BLE001 - metrics must never stop a process starting
        report_once(FILE_FENCE_CONFLICTS_TOTAL.name, exc)


def record_file_fence_conflict(*, file_operation: str, holder_kind: str) -> None:
    """Count an operation refused because another one held the file."""
    try:
        _instruments().conflicts.inc(1, {"file_operation": file_operation, "holder_kind": holder_kind})
    except Exception as exc:  # noqa: BLE001 - metrics must never fail an operation
        report_once(FILE_FENCE_CONFLICTS_TOTAL.name, exc)


def record_file_fence_commit_refused() -> None:
    """Count a catalog write refused because its operation lost the file's fence."""
    try:
        _instruments().commits_refused.inc(1, {})
    except Exception as exc:  # noqa: BLE001
        report_once(FILE_FENCE_COMMITS_REFUSED_TOTAL.name, exc)


def record_file_fence_renewal_failure(*, holder_kind: str) -> None:
    """Count a fence renewal that failed, whether it is retried or ends the operation."""
    try:
        _instruments().renewal_failures.inc(1, {"holder_kind": holder_kind})
    except Exception as exc:  # noqa: BLE001
        report_once(FILE_FENCE_RENEWAL_FAILURES_TOTAL.name, exc)


__all__ = [
    "record_file_fence_commit_refused",
    "record_file_fence_conflict",
    "record_file_fence_renewal_failure",
    "start_file_fence_series",
]
