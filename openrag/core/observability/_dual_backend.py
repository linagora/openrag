"""The two metric backends, and the rule for picking one per process.

Shared by every metric recorded on both sides of Ray (``inference_metrics``,
``file_fence_metrics``). ``inference_metrics``' module docstring explains the
rule: record where this process can actually be scraped, through exactly one
backend.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from core.observability._ray_counters import start_counter_at_zero


class Instrument(Protocol):
    """The two backends' APIs differ; call sites should not have to know which."""

    def inc(self, value: float, tags: dict[str, str]) -> None: ...

    def start_at_zero(self, tags: dict[str, str]) -> None: ...

    def observe(self, value: float, tags: dict[str, str]) -> None: ...

    def set(self, value: float, tags: dict[str, str]) -> None: ...


class RayInstrument:
    def __init__(self, metric: object) -> None:
        self._metric = metric

    def inc(self, value: float, tags: dict[str, str]) -> None:
        self._metric.inc(value, tags=tags)

    def start_at_zero(self, tags: dict[str, str]) -> None:
        start_counter_at_zero(self._metric, tags)

    def observe(self, value: float, tags: dict[str, str]) -> None:
        self._metric.observe(value, tags=tags)

    def set(self, value: float, tags: dict[str, str]) -> None:
        self._metric.set(value, tags=tags)


class PrometheusInstrument:
    def __init__(self, metric: object) -> None:
        self._metric = metric

    def _series(self, tags: dict[str, str]) -> object:
        # ``labels()`` raises on a metric declared without labels: its only
        # series is the metric itself.
        return self._metric.labels(**tags) if tags else self._metric

    def inc(self, value: float, tags: dict[str, str]) -> None:
        self._series(tags).inc(value)

    def start_at_zero(self, tags: dict[str, str]) -> None:
        self._series(tags).inc(0)

    def observe(self, value: float, tags: dict[str, str]) -> None:
        self._series(tags).observe(value)

    def set(self, value: float, tags: dict[str, str]) -> None:
        self._series(tags).set(value)


@lru_cache(maxsize=1)
def use_ray_backend() -> bool:
    """Whether this process exports through Ray rather than ``/metrics``.

    Cached: a process does not migrate between the two. A failure resolving
    Ray's context means we are not in an actor, so ``prometheus_client`` is the
    correct answer rather than an error.
    """
    try:
        import ray

        return ray.get_runtime_context().get_actor_id() is not None
    except Exception:  # noqa: BLE001 - absence of Ray is an answer, not a failure
        return False
