"""
Prometheus-compatible monitoring for OpenRAG.

Exposes request metrics (count, failures, duration histograms)
via prometheus_client.
"""

import threading
from collections.abc import Mapping

from core.models.readiness import ReadinessSnapshot
from core.observability.metric_specs import INGEST_TASK_STATE_VALUES
from core.observability.metric_specs import INGEST_TASKS as INGEST_TASKS_SPEC
from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Registry — use the default global registry so all metrics are auto-collected
# ---------------------------------------------------------------------------

# -- Request metrics --------------------------------------------------------

ORPHAN_CHUNKS_DROPPED = Counter(
    "openrag_retrieval_orphan_chunks_dropped_total",
    "Chunk-drop occurrences for files absent from the catalog; repeated retrievals can count the same chunk again",
)

REQUEST_COUNT = Counter(
    "openrag_http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"],
)

REQUEST_FAILURES = Counter(
    "openrag_http_request_failures_total",
    "Total number of failed HTTP requests (status >= 400)",
    ["method", "endpoint", "status_code"],
)

REQUEST_DURATION = Histogram(
    "openrag_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, float("inf")),
)


class ModelEndpointReadinessMetrics:
    """Synchronize the current bounded model-endpoint readiness series."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._ready = Gauge(
            "openrag_model_endpoint_ready",
            "Whether a default or referenced model endpoint is ready",
            ["provider", "kind"],
            registry=registry,
        )
        self._discovery_up = Gauge(
            "openrag_model_endpoint_discovery_up",
            "Whether the authoritative model endpoint snapshot was read successfully",
            registry=registry,
        )
        self._published: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._discovery_up.set(0)

    def publish(self, snapshot: ReadinessSnapshot) -> None:
        current = {(endpoint.provider, endpoint.kind): endpoint.status for endpoint in snapshot.model_endpoints}
        with self._lock:
            for provider, kind in self._published - current.keys():
                self._ready.remove(provider, kind)
            for (provider, kind), status in current.items():
                self._ready.labels(provider=provider, kind=kind).set(status == "ok")
            self._published = set(current)
            self._discovery_up.set(snapshot.checks.get("model_endpoint_discovery") == "ok")


MODEL_ENDPOINT_READINESS_METRICS = ModelEndpointReadinessMetrics()


def record_request(method: str, path: str, status_code: int, duration: float) -> None:
    """Record metrics for a completed HTTP request."""
    sc = str(status_code)
    REQUEST_COUNT.labels(method=method, endpoint=path, status_code=sc).inc()
    if status_code >= 400:
        REQUEST_FAILURES.labels(method=method, endpoint=path, status_code=sc).inc()
    REQUEST_DURATION.labels(method=method, endpoint=path).observe(duration)


# -- Ingestion backlog ------------------------------------------------------
# Sampled at scrape time rather than maintained by hand: the authoritative
# count lives in the TaskStateManager actor, and a gauge incremented on each
# transition would drift from it on every actor restart, cancellation or fenced
# task. Built from the shared spec so it cannot diverge from the worker-side
# metrics in name or labels.

INGEST_TASKS = Gauge(
    INGEST_TASKS_SPEC.name,
    INGEST_TASKS_SPEC.description,
    list(INGEST_TASKS_SPEC.labels),
)


def set_ingest_task_counts(counts: Mapping[str, int]) -> None:
    """Publish the in-flight task counts.

    Only the declared states are published. ``counts`` originates from the task
    state machine, so an unexpected key would be a bug rather than an attack —
    but it would still mint a Prometheus series, and silently dropping it keeps
    that impossible by construction rather than by review.

    Every declared state is written on every call, including zeros: an absent
    series and a genuinely empty queue must not look the same on a dashboard.
    """
    for state in INGEST_TASK_STATE_VALUES:
        INGEST_TASKS.labels(state=state).set(counts.get(state, 0))


def clear_ingest_task_counts() -> None:
    """Withdraw the gauge when the count could not be read.

    Leaving the previous values in place would report a stale backlog as the
    current one, which is worse than reporting nothing: the series simply goes
    absent and the panel shows no data.
    """
    INGEST_TASKS.clear()


def get_metrics() -> bytes:
    """Return all metrics in Prometheus text exposition format."""
    return generate_latest()
