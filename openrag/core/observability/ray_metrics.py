"""Worker-side metric instances, instantiated from the shared specs.

The counterpart to :mod:`core.observability.monitoring`. That module serves the
API process through ``prometheus_client`` and ``/metrics``; this one serves
everything produced inside a Ray actor, where a ``prometheus_client`` counter
would be incremented into a registry nothing ever scrapes.

Both instantiate from :mod:`core.observability.metric_specs`, so the two
backends cannot drift in name, label set or bucket layout.

**How these reach Prometheus.** Ray's per-node metrics agent collects from every
local actor and exposes them on the node's ``MetricsExportPort``
(``--metrics-export-port=8090``, see ``infra/charts/openrag-stack/templates/raycluster.yaml``),
scraped by the chart's ``PodMonitor``. Ray prefixes every name with ``ray_``, and
that ``PodMonitor`` strips it again: a spec named
``openrag_ingest_documents_total`` is exported as
``ray_openrag_ingest_documents_total`` and queried under its spec name, like the
API's own series.

**Querying them.** Ray attaches ``WorkerId``, ``SessionName``, ``NodeAddress``,
``Component`` and ``Version``. Always aggregate those away and always rate
before summing::

    sum without(WorkerId, SessionName, NodeAddress, Component, Version) (
        rate(openrag_ingest_documents_total[5m])
    )

Per-worker counters reset when an actor dies, and ``rate()`` only detects a
reset within a single series — ``rate(sum(...))`` silently loses those resets.
Never drop ``WorkerId`` at scrape time: it is what keeps concurrent workers'
series distinct, and collapsing them puts duplicate samples in one scrape, which
Prometheus rejects wholesale.

**Recording is best-effort, always.** Every function here swallows its own
errors. An indexing task must never fail because a metric could not be written —
and ``observe_stage_duration`` is called from a ``finally`` block, where a raise
would mask the pipeline's real exception. Failures are warned once per metric,
so a broken one is visible without flooding a log that these run against on
every document.

**A caveat on silence.** ``ray.util.metrics`` records happily when Ray is not
initialised — it is a no-op, not an error. That keeps imports safe in unit tests
and in the API process, but it also means "no series in Prometheus" and "this
code never ran under Ray" look identical from the outside. The unit tests
therefore assert that each call site is reached, which is the only failure mode
that can be caught without a live Ray cluster.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from core.observability._ray_counters import start_counter_at_zero
from core.observability._reporting import report_once
from core.observability.metric_specs import (
    INGEST_CLOCK_SKEW_TOTAL,
    INGEST_DOCUMENTS_TOTAL,
    INGEST_LAST_PARSE_TIMESTAMP,
    INGEST_QUEUE_WAIT_SECONDS,
    INGEST_STAGE_DURATION_SECONDS,
    INGEST_STATUS_VALUES,
    MetricSpec,
)
from ray.util.metrics import Counter, Gauge, Histogram


def _counter(spec: MetricSpec) -> Counter:
    return Counter(spec.name, description=spec.description, tag_keys=spec.labels)


def _histogram(spec: MetricSpec) -> Histogram:
    return Histogram(spec.name, description=spec.description, boundaries=list(spec.buckets), tag_keys=spec.labels)


def _gauge(spec: MetricSpec) -> Gauge:
    return Gauge(spec.name, description=spec.description, tag_keys=spec.labels)


# Module-level singletons. Ray metrics are cheap to construct and safe to build
# without ``ray.init()``, so this needs no lazy-initialisation machinery.
_DOCUMENTS_TOTAL = _counter(INGEST_DOCUMENTS_TOTAL)
_STAGE_DURATION = _histogram(INGEST_STAGE_DURATION_SECONDS)
_QUEUE_WAIT = _histogram(INGEST_QUEUE_WAIT_SECONDS)
_CLOCK_SKEW_TOTAL = _counter(INGEST_CLOCK_SKEW_TOTAL)
_LAST_PARSE_TIMESTAMP = _gauge(INGEST_LAST_PARSE_TIMESTAMP)


def initialize_ingest_counters() -> None:
    """Start the ingest counters at 0 in the process that will record them.

    Called when the TaskStateManager starts. Without it each status series
    first appears at 1, which ``increase()`` does not count: the first failure
    after every restart was missing from the failure ratio
    ``OpenRagIngestFailureRate`` reads.
    """
    try:
        for status in INGEST_STATUS_VALUES:
            start_counter_at_zero(_DOCUMENTS_TOTAL, {"status": status})
        start_counter_at_zero(_CLOCK_SKEW_TOTAL)
    except Exception as exc:  # noqa: BLE001 - metrics must never stop the actor starting
        report_once(INGEST_DOCUMENTS_TOTAL.name, exc)


def record_document_terminal(status: str) -> None:
    """Count one document reaching a terminal indexing state.

    ``status`` is lowercased so the label matches ``INGEST_STATUS_VALUES``
    whichever casing the caller's state machine uses.

    Callers must invoke this only on an actual *transition* into a terminal
    state. The TaskStateManager's setters are re-entrant — a retried actor call
    can set ``FAILED`` on an already-failed task — and counting the write rather
    than the transition would inflate the failure rate that S3-4 alerts on.
    """
    try:
        _DOCUMENTS_TOTAL.inc(1, tags={"status": str(status).lower()})
    except Exception as exc:  # noqa: BLE001 - metrics must never break indexing
        report_once(INGEST_DOCUMENTS_TOTAL.name, exc)


def observe_stage_duration(stage: str, seconds: float) -> None:
    """Record how long one pipeline stage took.

    ``pipeline_builder`` measures in milliseconds internally; this takes
    seconds, which is the Prometheus base unit the metric is named for.
    """
    try:
        _STAGE_DURATION.observe(float(seconds), tags={"stage": stage})
    except Exception as exc:  # noqa: BLE001
        report_once(INGEST_STAGE_DURATION_SECONDS.name, exc)


def observe_queue_wait_from(created_at: str | None, *, now: datetime | None = None) -> None:
    """Record admission-to-processing latency, given the ISO ``created_at``.

    The two timestamps are taken in different processes — ``created_at`` by the
    dispatcher in the API process, ``now`` by whichever actor observes the
    transition — which under Kubernetes means different nodes and therefore
    unsynchronised clocks.

    A negative interval is physically impossible, so it can only mean skew. It
    is clamped to zero and counted separately rather than dropped: dropping it
    would quietly bias the histogram, and folding it into the zero bucket
    without a counter would make a skewed fleet look like an idle queue. The
    named counter turns "our clocks disagree" into something a dashboard can
    say out loud.

    A missing or unparseable ``created_at`` records nothing at all — an absent
    observation is honest, a zero would be a lie that drags the p50 down.
    """
    if not isinstance(created_at, str):
        return
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)

    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    waited = (reference - created).total_seconds()
    try:
        if waited < 0:
            _CLOCK_SKEW_TOTAL.inc(1)
            waited = 0.0
        _QUEUE_WAIT.observe(waited)
    except Exception as exc:  # noqa: BLE001
        report_once(INGEST_QUEUE_WAIT_SECONDS.name, exc)


def record_parse_completion(pool: str, *, at: float | None = None) -> bool:
    """Stamp the time a parse finished, per parser backend.

    Exports a *timestamp*, not an age. An age gauge has to be rewritten
    continuously to stay truthful and reads as zero whenever nothing updates
    it — which is exactly the wedged-pool condition the watchdog exists to
    detect (a pool whose workers are busy but completing nothing stops updating
    the metric, so a "seconds since" value would freeze rather than climb).

    Exporting the completion time instead lets the alert compute the age at
    evaluation, which climbs on its own while the pool is stuck::

        time() - max without(WorkerId, SessionName, NodeAddress, Component, Version) (
            openrag_ingest_last_parse_completion_timestamp_seconds
        ) > 300

    Returns whether the stamp was written; a failure is reported, never raised.
    """
    try:
        _LAST_PARSE_TIMESTAMP.set(float(at if at is not None else time.time()), tags={"pool": pool})
    except Exception as exc:  # noqa: BLE001
        report_once(INGEST_LAST_PARSE_TIMESTAMP.name, exc)
        return False
    return True


_WATCHDOG_SEEDED: set[str] = set()


def seed_parse_watchdog(pool: str) -> None:
    """Stamp a pool's first use in this process, once.

    ``record_parse_completion`` stamps successes only, so a pool that fails from
    its very first parse never produces the series, and an alert computing
    ``time() - max(<stamp>)`` has nothing to evaluate: the stall that starts at
    boot is the one it cannot see. Seeding at first use gives the stamp a start,
    and a pool that then completes nothing ages from there like any other.

    Marked seeded only once the stamp is written: a failed write would
    otherwise use up the one seed, and a pool that also never completes would
    be left without a series again.
    """
    if pool in _WATCHDOG_SEEDED:
        return
    if record_parse_completion(pool):
        _WATCHDOG_SEEDED.add(pool)


__all__ = [
    "observe_queue_wait_from",
    "observe_stage_duration",
    "record_document_terminal",
    "record_parse_completion",
    "seed_parse_watchdog",
]
