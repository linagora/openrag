"""
Prometheus-compatible monitoring for OpenRAG.

Exposes request metrics (count, failures, duration histograms)
via prometheus_client.
"""

from prometheus_client import (
    Counter,
    Histogram,
    generate_latest,
)

# ---------------------------------------------------------------------------
# Registry — use the default global registry so all metrics are auto-collected
# ---------------------------------------------------------------------------

# -- Request metrics --------------------------------------------------------

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
