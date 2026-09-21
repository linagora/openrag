"""
Prometheus-compatible monitoring for OpenRAG.

Exposes request metrics (count, failures, duration histograms)
via prometheus_client.
"""

import threading

from core.models.readiness import ReadinessSnapshot
from core.observability.canary import CanaryRunResult, CanaryStage
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


class CanaryMetrics:
    """Outcome of the synthetic canary, exported by the replica that runs it.

    Every replica exports these series; only the one holding the canary lease
    runs it. Timestamps keep their last value when a replica loses the lease,
    so ``max()`` across replicas is the most recent run anywhere. The failure
    streak resets instead: a former runner must not keep an alert firing for
    runs it no longer makes.
    """

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._enabled = Gauge(
            "openrag_canary_enabled",
            "Whether the synthetic canary is configured to run on this replica",
            registry=registry,
        )
        self._interval = Gauge(
            "openrag_canary_interval_seconds",
            "Configured seconds between synthetic canary runs",
            registry=registry,
        )
        self._leader = Gauge(
            "openrag_canary_leader",
            "Whether this replica holds the canary lease and runs the canary",
            registry=registry,
        )
        self._runs = Counter(
            "openrag_canary_runs_total",
            "Synthetic canary runs by outcome",
            ["outcome"],
            registry=registry,
        )
        self._failures = Counter(
            "openrag_canary_failures_total",
            "Failed synthetic canary runs by the stage that failed",
            ["stage"],
            registry=registry,
        )
        self._consecutive_failures = Gauge(
            "openrag_canary_consecutive_failures",
            "Failed synthetic canary runs since the last successful one, on the runner",
            registry=registry,
        )
        self._last_run = Gauge(
            "openrag_canary_last_run_timestamp_seconds",
            "Unix time the last synthetic canary run finished, whatever its outcome; 0 before the first",
            registry=registry,
        )
        self._last_success = Gauge(
            "openrag_canary_last_success_timestamp_seconds",
            "Unix time the last synthetic canary run passed; 0 before the first",
            registry=registry,
        )
        self._duration = Gauge(
            "openrag_canary_stage_duration_seconds",
            "Seconds the last synthetic canary run spent in each stage; stage=total is the whole run",
            ["stage"],
            registry=registry,
        )
        self._streak = 0
        self._lock = threading.Lock()
        # Materialize every bounded label value so rate() starts from zero
        # rather than from the first failure.
        for outcome in ("success", "failure"):
            self._runs.labels(outcome=outcome)
        for stage in CanaryStage:
            self._failures.labels(stage=stage.value)

    def configure(self, *, enabled: bool, interval_seconds: float) -> None:
        self._enabled.set(1 if enabled else 0)
        self._interval.set(interval_seconds)

    def set_leader(self, leader: bool) -> None:
        with self._lock:
            self._leader.set(1 if leader else 0)
            if not leader:
                self._streak = 0
                self._consecutive_failures.set(0)

    def record(self, result: CanaryRunResult, *, finished_at: float) -> None:
        with self._lock:
            self._runs.labels(outcome="success" if result.passed else "failure").inc()
            if result.passed:
                self._streak = 0
                self._last_success.set(finished_at)
            else:
                self._streak += 1
                if result.failed_stage is not None:
                    self._failures.labels(stage=result.failed_stage.value).inc()
            self._consecutive_failures.set(self._streak)
            self._last_run.set(finished_at)
            # Stages the run never reached read 0, not the previous run's value.
            for stage in CanaryStage:
                self._duration.labels(stage=stage.value).set(result.durations.get(stage, 0.0))
            self._duration.labels(stage="total").set(result.total_seconds)


CANARY_METRICS = CanaryMetrics()


def record_request(method: str, path: str, status_code: int, duration: float) -> None:
    """Record metrics for a completed HTTP request."""
    sc = str(status_code)
    REQUEST_COUNT.labels(method=method, endpoint=path, status_code=sc).inc()
    if status_code >= 400:
        REQUEST_FAILURES.labels(method=method, endpoint=path, status_code=sc).inc()
    REQUEST_DURATION.labels(method=method, endpoint=path).observe(duration)


def get_metrics() -> bytes:
    """Return all metrics in Prometheus text exposition format."""
    return generate_latest()
