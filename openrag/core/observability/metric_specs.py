"""Declarative specifications for every OpenRag metric — one source of truth.

**Why specs rather than metric objects.** S3-2's metrics are produced on two
sides of a process boundary. The HTTP and chat-path metrics live in the API
process and use ``prometheus_client``; the per-document ``ingest_*`` metrics
are produced inside a Ray actor (``services/workers/indexer_actor.py``, driven
by ``indexer_pool.py``) and must use ``ray.util.metrics``, because a
``prometheus_client`` counter incremented in a Ray worker is invisible to
``/metrics`` in the API process — there is no shared registry, and under
``ENABLE_RAY_SERVE=true`` (``values-linagora.yaml``) even the API runs as Serve
replicas that HTTP cannot address individually. The exception is
``openrag_ingest_tasks``: a gauge the API process samples from the task state
at scrape time, so it is served on the API's ``/metrics`` like the HTTP metrics.

``openrag_circuit_breaker_state`` (``services/inference/_circuit_breaker.py``)
is the existing instance of that bug: it is a ``prometheus_client`` Gauge set
from inside workers, so it never reaches a scrape. Declaring both backends from
one spec set is what stops the next six metrics repeating it.

**Why the labels are the important part.** A metric declared with an unbounded
label is not a monitoring bug, it is an availability bug — for the *platform's*
Prometheus, not just ours. ``partition`` is created on write by callers, so it
is bounded by user behaviour rather than by anything we control:
``openrag_ingest_stage_duration_seconds{partition,stage}`` is ~10 500 series at
150 partitions and ~700 000 at 10 000. :data:`FORBIDDEN_LABELS` makes that a
build-time failure (see ``tests/unit/core/observability/test_metric_cardinality.py``)
rather than something discovered when a shared Prometheus runs out of memory.

Questions that genuinely need a tenant breakdown are answered elsewhere by
design: "which partition is failing?" from structured logs (S3-11 binds
``partition`` as a field), "how much has this tenant consumed?" from Postgres
(D2 usage accounting).

**Cardinality of the worker-side set.** Ray injects its own labels —
``WorkerId``, ``SessionName``, ``NodeAddress``, ``Component``, ``Version`` — and
``WorkerId`` changes with every actor process, so each restart mints a fresh
series set. That churn is bounded, not unbounded: Ray's metrics agent drops a
dead worker's series after ``RAY_WORKER_TIMEOUT_S`` (default 120 s, see
``ray/_private/metrics_agent.py``), measured at ~130 s end to end. The worst
case is therefore *restart rate × timeout window*, which at 10 indexer actors is
~2 400 series steady-state and a few hundred more per crash-looping actor.

Two consequences for anyone querying these (S3-4 alert rules, S3-5 dashboards):

* Aggregate with ``sum without(WorkerId, SessionName, NodeAddress, Component,
  Version) (...)``. Recording rules are the right place to write that once.
* Always ``sum(rate(x[5m]))``, never ``rate(sum(x))`` — per-worker counters
  reset when an actor dies, and ``rate()`` only detects a reset within a series.
* Never drop ``WorkerId`` via ``metric_relabel_configs``. It is what keeps
  concurrent workers' series distinct; collapsing them makes one scrape contain
  duplicate samples, which Prometheus rejects wholesale. The saving is not worth
  losing the entire scrape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# The cardinality rule
# ---------------------------------------------------------------------------

#: Labels no metric may declare, on either backend. Each is bounded by user or
#: request behaviour rather than by configuration, so the series count follows
#: traffic instead of deployment size. ``D2`` already made this call for
#: ``user_id``; the rest follow the same reasoning.
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {
        "partition",  # created on write by callers — the headline case
        "user_id",
        "file_id",
        "task_id",
        "request_id",
        "filename",
    }
)


class ForbiddenLabelError(ValueError):
    """A metric declared a label from :data:`FORBIDDEN_LABELS`.

    Raised at instantiation as well as asserted in the unit test: the test is
    the build-time gate, this is the backstop for a metric created dynamically
    or outside the spec set.
    """


MetricKind = Literal["counter", "gauge", "histogram"]


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Backend-agnostic description of one metric.

    Instantiated against ``prometheus_client`` in the API process and against
    ``ray.util.metrics`` in workers. Holding the definition as data is what lets
    one test assert the cardinality rule across both.
    """

    name: str
    description: str
    kind: MetricKind
    labels: tuple[str, ...] = ()
    #: Upper bounds, excluding the implicit ``+Inf``. Histograms only.
    buckets: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        offenders = sorted(set(self.labels) & FORBIDDEN_LABELS)
        if offenders:
            raise ForbiddenLabelError(
                f"metric {self.name!r} declares forbidden label(s) {offenders}; "
                f"these are unbounded by design — see FORBIDDEN_LABELS in "
                f"core/observability/metric_specs.py for where such questions are answered instead"
            )
        if self.kind == "histogram" and not self.buckets:
            raise ValueError(f"histogram {self.name!r} must declare explicit buckets")
        if self.kind != "histogram" and self.buckets:
            raise ValueError(f"{self.kind} {self.name!r} must not declare buckets")
        if self.buckets and list(self.buckets) != sorted(self.buckets):
            raise ValueError(f"histogram {self.name!r} buckets must be ascending")


# ---------------------------------------------------------------------------
# Bucket sets — chosen per metric, because one set cannot serve all of them
# ---------------------------------------------------------------------------

#: Stage durations span four orders of magnitude: ``chunk`` completes in
#: milliseconds while a Marker ``parse`` of a large PDF runs for minutes. A
#: single geometric set covering both keeps the p95 of each readable; the
#: 300/600 s tail exists because a wedged parse is the failure S1's incident
#: review is about, and it must land in a bucket rather than in ``+Inf``.
INGEST_STAGE_BUCKETS: tuple[float, ...] = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

#: Queue wait is a backlog signal, not a latency one: seconds when idle, but
#: tens of minutes during a backfill. Buckets are coarse and reach two hours so
#: "the backlog is growing" stays visible instead of saturating ``+Inf``.
INGEST_QUEUE_WAIT_BUCKETS: tuple[float, ...] = (1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0)

#: Inference calls: an embed is tens of milliseconds, a long-context chat
#: completion is tens of seconds. The 120 s tail sits above the client timeouts
#: so a call that times out is distinguishable from one that merely ran long.
INFERENCE_DURATION_BUCKETS: tuple[float, ...] = (
    0.01,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)


# ---------------------------------------------------------------------------
# Bounded label value sets
# ---------------------------------------------------------------------------
# Declared here so dashboards, alert rules and tests share one enumeration
# rather than three copies of the same string literals. Each mirrors a value set
# that already exists in the code — the reference is given so a drift shows up
# as a failing test rather than as an empty dashboard panel.

#: ``services/workers/stages/`` module names, in pipeline order.
STAGE_VALUES: tuple[str, ...] = ("parse", "caption", "chunk", "contextualize", "topic_tag", "embed", "store")

#: Terminal values of ``core/models/catalog.DocumentStatus``. ``QUEUED`` and
#: ``SERIALIZING`` are deliberately absent: they are in-flight states, counted
#: by the ``openrag_ingest_tasks`` gauge rather than by a terminal counter.
INGEST_STATUS_VALUES: tuple[str, ...] = ("completed", "failed", "cancelled")

#: ``services/workers/task_state.ACTIVE_INDEXING_STATES``.
INGEST_TASK_STATE_VALUES: tuple[str, ...] = ("QUEUED", "SERIALIZING")

#: Parser backend names from ``parsers/parser_dispatcher._PDF_BACKENDS`` and
#: ``_AUDIO_BACKENDS``. Configuration-bounded, not traffic-bounded. Every other
#: format is stamped under its ``DocumentType`` value (``text``, ``docx``, ...),
#: which is bounded by the enum.
PARSER_POOL_VALUES: tuple[str, ...] = (
    "marker",
    "docling",
    "pymupdf",
    "pdf_client",
    "local_whisper",
    "audio_client",
)

#: What kind of call was made, not which model served it. ``provider`` carries
#: the registry *name*, which is admin-created and therefore bounded — the same
#: reasoning S3-1 applies to its per-endpoint readiness metric.
INFERENCE_OPERATION_VALUES: tuple[str, ...] = ("embed", "chat", "completion", "rerank", "vlm")

#: Bounded outcome enum. Never an exception message: that is the one place this
#: design could blow up cardinality from the value side rather than the key side.
#: ``cancelled`` is the caller giving up — a client closing a stream, a caller's
#: own deadline (``asyncio.wait_for``), sibling batches cancelled after one
#: failed. The provider did nothing wrong, so it must stay out of the error
#: ratio the provider alerts threshold on.
INFERENCE_OUTCOME_VALUES: tuple[str, ...] = ("success", "error", "timeout", "circuit_open", "cancelled", "rejected")

#: Token direction.
TOKEN_KIND_VALUES: tuple[str, ...] = ("prompt", "completion")


# ---------------------------------------------------------------------------
# Tier 1 — produced in Ray workers (exported via ray.util.metrics)
# ---------------------------------------------------------------------------

INGEST_DOCUMENTS_TOTAL = MetricSpec(
    name="openrag_ingest_documents_total",
    description="Documents that reached a terminal indexing state, by outcome",
    kind="counter",
    labels=("status",),
)

INGEST_STAGE_DURATION_SECONDS = MetricSpec(
    name="openrag_ingest_stage_duration_seconds",
    description="Wall-clock duration of each indexing pipeline stage",
    kind="histogram",
    labels=("stage",),
    buckets=INGEST_STAGE_BUCKETS,
)

INGEST_QUEUE_WAIT_SECONDS = MetricSpec(
    name="openrag_ingest_queue_wait_seconds",
    description="Time from task admission to the start of processing",
    kind="histogram",
    labels=(),
    buckets=INGEST_QUEUE_WAIT_BUCKETS,
)

#: A *timestamp* gauge, not a "seconds since" one. A seconds-since gauge has to
#: be rewritten continuously to stay truthful and reads as 0 whenever nothing
#: updates it — which is precisely the wedged-pool condition it exists to
#: detect. Exporting the completion time instead lets the alert compute the age
#: at evaluation: ``time() - max without(...)(...) > 5 * baseline``.
INGEST_LAST_PARSE_TIMESTAMP = MetricSpec(
    name="openrag_ingest_last_parse_completion_timestamp_seconds",
    description="Unix timestamp of the most recent completed parse, per parser pool",
    kind="gauge",
    labels=("pool",),
)

#: Clock skew guard. ``queue_wait`` is the difference between a timestamp set in
#: the API process (``dispatcher.py``) and one read in a worker process, which
#: on Kubernetes is a different node. A negative result is clamped to zero and
#: counted here, so a skewed fleet shows up as a named condition instead of
#: quietly flattening the queue-wait histogram's low bucket.
INGEST_CLOCK_SKEW_TOTAL = MetricSpec(
    name="openrag_ingest_clock_skew_events_total",
    description="Queue-wait measurements that came out negative, indicating clock skew between API and worker nodes",
    kind="counter",
    labels=(),
)


# ---------------------------------------------------------------------------
# Tier 1 — produced in the API process (exported via prometheus_client)
# ---------------------------------------------------------------------------

#: Sampled at scrape time from ``job_service.get_active_task_counts()`` rather
#: than incremented on transitions: a counter maintained by hand would drift on
#: every restart, cancellation or fenced task. The count is durable-first — the
#: ``jobs`` table is authoritative, reconciled with the live TaskStateManager —
#: so tasks a restarted actor has forgotten still count; the actor alone is
#: used only while Postgres cannot answer.
INGEST_TASKS = MetricSpec(
    name="openrag_ingest_tasks",
    description="Tasks currently in flight, by state",
    kind="gauge",
    labels=("state",),
)


# ---------------------------------------------------------------------------
# Tier 1 — produced on BOTH sides
# ---------------------------------------------------------------------------
# Chat and rerank are called from the API process; embed and vlm are called from
# inside the indexing pipeline. The same spec is instantiated against each
# backend, so a query sums one metric name across both scrape targets.

INFERENCE_REQUESTS_TOTAL = MetricSpec(
    name="openrag_inference_requests_total",
    description="Calls to an external inference endpoint, by outcome",
    kind="counter",
    labels=("provider", "operation", "outcome"),
)

INFERENCE_DURATION_SECONDS = MetricSpec(
    name="openrag_inference_duration_seconds",
    description="Latency of calls to external inference endpoints",
    kind="histogram",
    labels=("provider", "operation"),
    buckets=INFERENCE_DURATION_BUCKETS,
)

#: Breaker state per configured breaker. ``name`` is one of the four breaker
#: names declared in ``services/inference`` (``llm``, ``embedder``, ``vlm``,
#: ``reranker``) — code-defined, so bounded by the source rather than by
#: configuration or traffic.
CIRCUIT_BREAKER_STATE = MetricSpec(
    name="openrag_circuit_breaker_state",
    description="Circuit breaker state (0=closed, 1=open, 2=half-open, -1=unknown)",
    kind="gauge",
    labels=("name",),
)

#: Aggregate token burn. Deliberately not per-tenant: "how much has this tenant
#: consumed?" is a billing question answered from Postgres by D2, and answering
#: it here would reintroduce ``partition`` through the back door.
LLM_TOKENS_TOTAL = MetricSpec(
    name="openrag_llm_tokens_total",
    description="Tokens consumed by LLM calls, by direction",
    kind="counter",
    labels=("operation", "kind"),
)


#: Every spec declared above. The cardinality test walks this; the backends
#: instantiate from it. A metric that is not here is not covered by either.
ALL_SPECS: tuple[MetricSpec, ...] = (
    CIRCUIT_BREAKER_STATE,
    INGEST_DOCUMENTS_TOTAL,
    INGEST_STAGE_DURATION_SECONDS,
    INGEST_QUEUE_WAIT_SECONDS,
    INGEST_LAST_PARSE_TIMESTAMP,
    INGEST_CLOCK_SKEW_TOTAL,
    INGEST_TASKS,
    INFERENCE_REQUESTS_TOTAL,
    INFERENCE_DURATION_SECONDS,
    LLM_TOKENS_TOTAL,
)


__all__ = [
    "ALL_SPECS",
    "CIRCUIT_BREAKER_STATE",
    "FORBIDDEN_LABELS",
    "INFERENCE_DURATION_SECONDS",
    "INFERENCE_OPERATION_VALUES",
    "INFERENCE_OUTCOME_VALUES",
    "INFERENCE_REQUESTS_TOTAL",
    "INGEST_CLOCK_SKEW_TOTAL",
    "INGEST_DOCUMENTS_TOTAL",
    "INGEST_LAST_PARSE_TIMESTAMP",
    "INGEST_QUEUE_WAIT_SECONDS",
    "INGEST_STAGE_DURATION_SECONDS",
    "INGEST_STATUS_VALUES",
    "INGEST_TASKS",
    "INGEST_TASK_STATE_VALUES",
    "LLM_TOKENS_TOTAL",
    "PARSER_POOL_VALUES",
    "STAGE_VALUES",
    "TOKEN_KIND_VALUES",
    "ForbiddenLabelError",
    "MetricKind",
    "MetricSpec",
]
