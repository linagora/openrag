"""Worker-side metrics must actually arrive on Ray's metrics endpoint.

Every other test in this suite asserts that a *call* was made. None of them can
fail the way this task's metrics really fail. ``ray.util.metrics`` records
happily when Ray is not initialised — it is a no-op, not an error — so a metric
wired to the wrong backend, or recorded in a process nothing scrapes, raises
nothing anywhere. It simply never appears, and the first symptom is an empty
dashboard panel weeks later.

``openrag_circuit_breaker_state`` was exactly that: a ``prometheus_client``
Gauge set from inside Ray workers, where ``/metrics`` does not exist. It looked
instrumented, passed review, and could never have produced a sample for the
embedder or VLM breakers.

This test therefore starts a real (embedded) Ray, drives every worker-side
recorder from inside an actor, scrapes the node's metrics agent over HTTP, and
asserts the series are present in the exposition text. It is the only check here
that would have caught that defect.

Marked ``slow``: Ray's exporter flushes on an interval, so this costs seconds
rather than milliseconds.
"""

from __future__ import annotations

import os
import pathlib
import re
import time
import urllib.request

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# Ray prefixes every metric it exports.
_PREFIX = "ray_openrag_"

# Names a metric may never be labelled by. Asserted against the *exposition
# text* rather than our declarations, so it also covers labels Ray injects on
# its own — a source the unit-level cardinality guard structurally cannot see.
_FORBIDDEN_LABELS = ("partition", "user_id", "file_id", "task_id", "request_id", "filename")


def _scrape(address: str, port: int) -> str:
    with urllib.request.urlopen(f"http://{address}:{port}/metrics", timeout=10) as response:
        return response.read().decode()


@pytest.fixture(scope="module")
def exported_metrics() -> str:
    """Drive every worker-side recorder inside an actor, then scrape the node."""
    os.environ.setdefault("RAY_metrics_report_interval_ms", "2000")
    ray = pytest.importorskip("ray")

    # A Ray worker is a fresh process and does not inherit pytest's
    # ``pythonpath`` setting, so ``openrag/`` has to be put on its path
    # explicitly. In production the package is installed in the Ray image and
    # this is unnecessary — it is harness plumbing, not behaviour under test.
    package_root = pathlib.Path(__file__).resolve().parents[2] / "openrag"

    # A session started earlier in this pytest process would silently ignore
    # the runtime_env below, and the actor would fail to import ``core``. Own
    # the session instead of borrowing it.
    if ray.is_initialized():
        ray.shutdown()
    ray.init(
        num_cpus=2,
        include_dashboard=False,
        object_store_memory=150 * 1024 * 1024,
        log_to_driver=False,
        runtime_env={"env_vars": {"PYTHONPATH": str(package_root)}},
    )
    try:

        @ray.remote(num_cpus=1)
        class _Recorder:
            def record_everything(self) -> bool:
                # Imported inside the actor: this is the process whose export
                # path is under test, and the backend is chosen per process.
                from core.observability.inference_metrics import (
                    record_circuit_breaker_state,
                    record_inference,
                    record_tokens,
                    set_provider_name,
                )
                from core.observability.ray_metrics import (
                    initialize_ingest_counters,
                    observe_queue_wait_from,
                    observe_stage_duration,
                    record_document_terminal,
                    record_parse_completion,
                )
                from services.inference._metrics import with_inference_metrics

                class _ZeroStartClient:
                    @with_inference_metrics("rerank")
                    async def rerank(self) -> None: ...

                # Series started at 0: nothing below ever cancels a document or
                # calls this client, so only the zero start can produce them.
                initialize_ingest_counters()
                set_provider_name(_ZeroStartClient(), "zero-start")

                record_document_terminal("COMPLETED")
                record_document_terminal("FAILED")
                observe_stage_duration("parse", 1.5)
                observe_stage_duration("embed", 0.25)
                observe_queue_wait_from("2020-01-01T00:00:00+00:00")
                record_parse_completion("marker")
                record_inference(provider="default", operation="embed", outcome="success", duration_seconds=0.2)
                record_tokens(operation="chat", prompt=120, completion=30)
                record_circuit_breaker_state("embedder", 1)
                return True

        recorder = _Recorder.remote()
        assert ray.get(recorder.record_everything.remote()) is True

        node = next(n for n in ray.nodes() if n.get("Alive"))
        address, port = node["NodeManagerAddress"], node["MetricsExportPort"]

        deadline = time.time() + 90
        body = ""
        while time.time() < deadline:
            time.sleep(3)
            body = _scrape(address, port)
            if _PREFIX in body:
                break
        assert _PREFIX in body, "no openrag metric reached Ray's metrics endpoint within 90s"
        return body
    finally:
        ray.shutdown()


@pytest.mark.parametrize(
    ("metric", "label"),
    [
        ("ingest_documents_total", 'status="completed"'),
        ("ingest_documents_total", 'status="failed"'),
        ("ingest_stage_duration_seconds_count", 'stage="parse"'),
        ("ingest_stage_duration_seconds_count", 'stage="embed"'),
        ("ingest_queue_wait_seconds_count", ""),
        ("ingest_last_parse_completion_timestamp_seconds", 'pool="marker"'),
        ("inference_requests_total", 'operation="embed"'),
        ("llm_tokens_total", 'kind="prompt"'),
        # The regression guard. This metric was previously a prometheus_client
        # Gauge set from inside workers, so it could never produce a sample.
        ("circuit_breaker_state", 'name="embedder"'),
    ],
)
def test_metric_reaches_the_export_endpoint(exported_metrics: str, metric: str, label: str) -> None:
    matching = [line for line in exported_metrics.splitlines() if line.startswith(f"{_PREFIX}{metric}")]
    assert matching, f"{_PREFIX}{metric} is absent from the exposition"
    if label:
        assert any(label in line for line in matching), f"{_PREFIX}{metric} has no sample with {label}"


@pytest.mark.parametrize(
    ("metric", "labels"),
    [
        ("ingest_documents_total", ('status="cancelled"',)),
        ("inference_requests_total", ('provider="zero-start"', 'operation="rerank"', 'outcome="timeout"')),
    ],
)
def test_counters_are_exported_at_zero_before_their_first_event(
    exported_metrics: str, metric: str, labels: tuple[str, ...]
) -> None:
    """Pins the zero start on the wire. Ray's public ``Counter.inc`` refuses 0,
    so ``_ray_counters`` records through Ray's private ``_record``; a Ray upgrade
    that changes it fails here instead of silently losing every worker's first
    event to ``increase()`` again."""
    matching = [
        line
        for line in exported_metrics.splitlines()
        if line.startswith(f"{_PREFIX}{metric}") and all(label in line for label in labels)
    ]
    assert matching, f"no zero-started {metric} series with {labels}"
    assert all(float(line.rsplit(" ", 1)[1]) == 0 for line in matching), matching


@pytest.mark.parametrize("forbidden", _FORBIDDEN_LABELS)
def test_no_forbidden_label_is_exported(exported_metrics: str, forbidden: str) -> None:
    """End-to-end cardinality check, against what is really on the wire.

    The unit guard inspects what we declare. This inspects what Ray emits, so it
    also covers a label the framework attaches without being asked.
    """
    offenders = [
        line
        for line in exported_metrics.splitlines()
        if line.startswith(_PREFIX) and re.search(rf'[{{,]{forbidden}="', line)
    ]
    assert not offenders, f"{forbidden} was exported on: {offenders[:3]}"


def test_worker_identity_labels_are_present(exported_metrics: str) -> None:
    """Ray attaches ``WorkerId`` and ``SessionName``, and both churn.

    Pinned deliberately rather than merely documented: S3-4 and S3-5 must
    aggregate them away (``sum without(...)``), and dropping them at scrape time
    would collapse concurrent workers into duplicate samples and make Prometheus
    reject the whole scrape. A future Ray upgrade that changes these names
    silently breaks every recording rule written against them, and this is what
    reports it.
    """
    sample = next(line for line in exported_metrics.splitlines() if line.startswith(f"{_PREFIX}ingest_documents_total"))

    assert "WorkerId=" in sample
    assert "SessionName=" in sample
