"""The cardinality guard — a build-time gate on metric labels.

``tests/unit/api/middleware/test_instrumentation.py`` already pins one instance
of this rule: an unmatched URL collapses to a fixed ``/-not-found-`` rather than
minting one series per scanned path. These tests generalise it from "the
``endpoint`` label is safe" to "no metric may declare an unbounded label", so
the protection covers every future metric instead of the ones that exist today.

Why a test rather than a convention: the cost of getting this wrong is not paid
by us. In the integrated deployment our metrics land on the collaborative
platform's shared Prometheus, and the failure mode is not graceful degradation —
it is their monitoring team removing our ``ServiceMonitor``. A label added in
review on a Friday should fail CI, not a shared TSDB three months later.

Two layers, because there are two ways a metric can come into existence:

* every declared :class:`MetricSpec` (covers both backends — ``prometheus_client``
  in the API process, ``ray.util.metrics`` in Ray workers);
* every collector actually present in the live ``prometheus_client`` registry,
  which catches a metric defined directly against the client library without
  going through a spec.

The third set of tests pins the bounded label *values* against the code they
mirror. A drift there does not explode cardinality — it silently empties a
dashboard panel, which is the harder failure to notice.
"""

from __future__ import annotations

import pkgutil

import pytest
from core.observability.metric_specs import (
    ALL_SPECS,
    FORBIDDEN_LABELS,
    INFERENCE_OPERATION_VALUES,
    INGEST_STATUS_VALUES,
    INGEST_TASK_STATE_VALUES,
    PARSER_POOL_VALUES,
    STAGE_VALUES,
    ForbiddenLabelError,
    MetricSpec,
)

# ---------------------------------------------------------------------------
# Layer 1 — the declared specs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_no_spec_declares_a_forbidden_label(spec: MetricSpec) -> None:
    """No metric may be labelled by anything callers can mint.

    ``partition`` is the headline case: partitions are created on write, so the
    series count follows user behaviour rather than deployment size —
    ``openrag_ingest_stage_duration_seconds{partition,stage}`` is ~10 500 series
    at 150 partitions and ~700 000 at 10 000.
    """
    offenders = sorted(set(spec.labels) & FORBIDDEN_LABELS)
    assert not offenders, (
        f"{spec.name} declares {offenders}. Per-tenant questions are answered "
        f"outside Prometheus by design: 'which partition is failing?' from "
        f"structured logs (S3-11), 'how much has this tenant consumed?' from "
        f"Postgres (D2)."
    )


def test_spec_rejects_a_forbidden_label_at_construction() -> None:
    """The runtime backstop fires too, not just the test above.

    A metric built dynamically, or outside ``ALL_SPECS``, still cannot smuggle a
    forbidden label past the type.
    """
    with pytest.raises(ForbiddenLabelError, match="partition"):
        MetricSpec(
            name="openrag_test_bad_metric_total",
            description="should never be constructible",
            kind="counter",
            labels=("status", "partition"),
        )


def test_spec_names_are_unique() -> None:
    """Two specs sharing a name would mean one silently shadows the other on
    whichever backend registers second."""
    names = [s.name for s in ALL_SPECS]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"duplicate metric names: {duplicates}"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_counter_names_end_in_total(spec: MetricSpec) -> None:
    """Prometheus naming convention, and ``promtool check rules`` enforces it
    on anything that queries these."""
    if spec.kind == "counter":
        assert spec.name.endswith("_total"), f"{spec.name} is a counter and must end in _total"


@pytest.mark.parametrize("spec", ALL_SPECS, ids=lambda s: s.name)
def test_duration_histograms_are_named_in_seconds(spec: MetricSpec) -> None:
    """Base units, not milliseconds.

    ``pipeline_builder`` records stage timings in *milliseconds* internally; the
    exported metric must be seconds, so this pins the unit at the boundary where
    the conversion is easy to forget.
    """
    if spec.kind == "histogram":
        assert spec.name.endswith("_seconds"), f"{spec.name} is a duration histogram and must end in _seconds"


# ---------------------------------------------------------------------------
# Layer 2 — the live prometheus_client registry
# ---------------------------------------------------------------------------


def _import_modules_defining_metrics() -> None:
    """Import every module that registers a ``prometheus_client`` collector.

    Metrics register as a side effect of import, so the registry is only
    complete once these have been loaded. Imported by name rather than by
    walking the package: a blanket walk pulls in Ray, torch and the model
    stack, which makes a unit test slow and flaky for no extra coverage.
    """
    import core.observability.monitoring  # noqa: F401
    import services.inference._circuit_breaker  # noqa: F401


def test_live_registry_declares_no_forbidden_label() -> None:
    """Catches a metric created straight against ``prometheus_client``.

    The spec set cannot see those, and this is how the rule reaches them.
    """
    from prometheus_client import REGISTRY

    _import_modules_defining_metrics()

    offenders: dict[str, list[str]] = {}
    for collector in list(REGISTRY._collector_to_names):
        for name in getattr(collector, "_labelnames", ()) or ():
            if name in FORBIDDEN_LABELS:
                offenders.setdefault(getattr(collector, "_name", repr(collector)), []).append(name)

    assert not offenders, f"live registry has metrics with forbidden labels: {offenders}"


# ---------------------------------------------------------------------------
# Layer 3 — label values must not drift from the code they mirror
# ---------------------------------------------------------------------------
# These do not protect cardinality; they protect truthfulness. A stage renamed
# in `services/workers/stages/` without updating STAGE_VALUES leaves a dashboard
# panel permanently empty, which reads exactly like "nothing is happening".


def test_stage_values_match_the_stage_modules() -> None:
    import services.workers.stages as stages_pkg

    on_disk = {
        m.name.removesuffix("_stage") for m in pkgutil.iter_modules(stages_pkg.__path__) if not m.name.startswith("_")
    }
    assert set(STAGE_VALUES) == on_disk, (
        f"STAGE_VALUES {sorted(STAGE_VALUES)} has drifted from services/workers/stages/ {sorted(on_disk)}"
    )


def test_task_state_values_match_active_indexing_states() -> None:
    from services.workers.task_state import ACTIVE_INDEXING_STATES

    assert set(INGEST_TASK_STATE_VALUES) == set(ACTIVE_INDEXING_STATES)


def test_ingest_status_values_are_the_terminal_document_states() -> None:
    from core.models.catalog import TERMINAL_TASK_STATES

    assert set(INGEST_STATUS_VALUES) == {s.value.lower() for s in TERMINAL_TASK_STATES}


def test_parser_pool_values_match_the_dispatcher_backends() -> None:
    from services.workers.parsers.parser_dispatcher import _AUDIO_BACKENDS, _PDF_BACKENDS

    known = set(_PDF_BACKENDS.values()) | set(_AUDIO_BACKENDS.values())
    assert set(PARSER_POOL_VALUES) == known, (
        f"PARSER_POOL_VALUES {sorted(PARSER_POOL_VALUES)} has drifted from the "
        f"dispatcher's backend names {sorted(known)}"
    )


def test_the_pool_label_domain_is_closed() -> None:
    """The other half of what ``PARSER_POOL_VALUES`` documents.

    The check above pins the constant to the PDF and audio backends, but
    ``_resolve_backend`` also returns ``content_type.value`` for every other
    type, so ``pool`` carries ``text``, ``docx``, ``eml`` too. Those values were
    bounded only by the comment saying so. They are safe because
    ``DocumentType`` is an enum — a closed set fixed at import — which is the
    premise this asserts rather than restates. A ``DocumentType`` that stopped
    being an enum would make the label traffic-bounded and the cardinality
    argument for this metric false. That ``_resolve_backend`` draws its non-PDF
    and non-audio values from the enum is pinned by the dispatcher tests, not here.
    """
    from enum import Enum

    from core.models.document import DocumentType

    assert issubclass(DocumentType, Enum), (
        "DocumentType is no longer an enum, so the `pool` label's non-PDF/audio "
        "values are unbounded and the cardinality guarantee no longer holds."
    )

    from_types = {t.value for t in DocumentType} - {
        DocumentType.PDF.value,
        DocumentType.AUDIO.value,
        DocumentType.VIDEO.value,
    }
    domain = set(PARSER_POOL_VALUES) | from_types
    assert all(isinstance(v, str) and v.islower() and v.isidentifier() for v in domain), (
        f"every `pool` value must be a bounded identifier; got {sorted(domain)}"
    )


def test_inference_operation_values_are_bounded_and_lowercase() -> None:
    """``operation`` describes the kind of call, never the model name — a model
    name is client-controllable through ``metadata.llm_override``."""
    assert all(v.islower() and v.isidentifier() for v in INFERENCE_OPERATION_VALUES)
    assert len(set(INFERENCE_OPERATION_VALUES)) == len(INFERENCE_OPERATION_VALUES)


def test_every_emitted_inference_operation_is_declared() -> None:
    """`INFERENCE_OPERATION_VALUES` is what bounds the `operation` label, but
    nothing tied it to the values the clients actually emit: a new operation
    could ship undeclared. Every literal passed to `@with_inference_metrics(...)`
    or as `operation="..."` under `services/inference/` must be declared."""
    import pathlib
    import re

    from core.observability.metric_specs import INFERENCE_OPERATION_VALUES

    root = pathlib.Path(__file__).resolve().parents[4] / "openrag" / "services" / "inference"
    pattern = re.compile(r'with_inference_metrics\(\s*"([^"]+)"|operation="([^"]+)"')
    emitted = {a or b for path in root.rglob("*.py") for a, b in pattern.findall(path.read_text(encoding="utf-8"))}

    assert emitted, "found no operation literals: the pattern no longer matches the code"
    assert emitted <= set(INFERENCE_OPERATION_VALUES), (
        f"undeclared: {sorted(emitted - set(INFERENCE_OPERATION_VALUES))}"
    )
