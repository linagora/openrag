from core.observability.canary import CanaryRunResult, CanaryStage
from core.observability.monitoring import CanaryMetrics
from prometheus_client import CollectorRegistry

FORBIDDEN_LABELS = {"partition", "user_id", "file_id", "task_id", "request_id", "filename"}


def test_canary_series_exist_from_boot_with_bounded_labels_only():
    registry = CollectorRegistry()
    CanaryMetrics(registry=registry)

    samples = [sample for metric in registry.collect() for sample in metric.samples]
    # Every failure stage is materialized, so rate() starts at zero.
    for stage in CanaryStage:
        assert registry.get_sample_value("openrag_canary_failures_total", {"stage": stage.value}) == 0
    assert registry.get_sample_value("openrag_canary_last_run_timestamp_seconds") == 0
    for sample in samples:
        assert not (FORBIDDEN_LABELS & sample.labels.keys()), sample


def test_a_run_without_a_stage_counts_as_a_failure_but_names_no_stage():
    registry = CollectorRegistry()
    metrics = CanaryMetrics(registry=registry)

    metrics.record(CanaryRunResult(passed=False, reason="crashed"), finished_at=10.0)

    assert registry.get_sample_value("openrag_canary_runs_total", {"outcome": "failure"}) == 1
    assert registry.get_sample_value("openrag_canary_consecutive_failures") == 1
    assert all(
        registry.get_sample_value("openrag_canary_failures_total", {"stage": stage.value}) == 0 for stage in CanaryStage
    )
