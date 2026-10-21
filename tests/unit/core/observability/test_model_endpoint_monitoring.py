from core.models.readiness import ModelEndpointReadiness, ReadinessSnapshot
from core.observability.monitoring import ModelEndpointReadinessMetrics
from prometheus_client import CollectorRegistry, generate_latest


def test_endpoint_metrics_clear_failed_discovery_and_use_only_bounded_labels():
    registry = CollectorRegistry()
    metrics = ModelEndpointReadinessMetrics(registry=registry)
    metrics.publish(
        ReadinessSnapshot(
            checks={"model_endpoint_discovery": "ok"},
            model_endpoints=(
                ModelEndpointReadiness(provider="large-context", kind="llm", status="ok"),
                ModelEndpointReadiness(provider="transcriber", kind="stt", status="timeout"),
            ),
        )
    )

    first = generate_latest(registry).decode()
    assert 'openrag_model_endpoint_ready{kind="llm",provider="large-context"} 1.0' in first
    assert 'openrag_model_endpoint_ready{kind="stt",provider="transcriber"} 0.0' in first
    assert "openrag_model_endpoint_discovery_up 1.0" in first
    forbidden = {"partition", "model", "user_id", "file_id", "task_id", "request_id", "filename"}
    for sample in (sample for metric in registry.collect() for sample in metric.samples):
        assert not (forbidden & sample.labels.keys())

    metrics.publish(ReadinessSnapshot(checks={"model_endpoint_discovery": "unavailable"}))

    second = generate_latest(registry).decode()
    assert "large-context" not in second
    assert "transcriber" not in second
    assert "openrag_model_endpoint_discovery_up 0.0" in second


def test_successful_endpoint_refresh_replaces_stale_provider_series():
    registry = CollectorRegistry()
    metrics = ModelEndpointReadinessMetrics(registry=registry)
    metrics.publish(
        ReadinessSnapshot(
            checks={"model_endpoint_discovery": "ok"},
            model_endpoints=(ModelEndpointReadiness(provider="old", kind="llm", status="ok"),),
        )
    )

    metrics.publish(
        ReadinessSnapshot(
            checks={"model_endpoint_discovery": "ok"},
            model_endpoints=(ModelEndpointReadiness(provider="new", kind="llm", status="unavailable"),),
        )
    )

    output = generate_latest(registry).decode()
    assert 'provider="old"' not in output
    assert 'openrag_model_endpoint_ready{kind="llm",provider="new"} 0.0' in output
    assert "openrag_model_endpoint_discovery_up 1.0" in output
