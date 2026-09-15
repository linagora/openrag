"""Inference metrics — the one Tier-1 set produced on both sides of Ray.

``ingest_*`` metrics only ever happen in a worker and ``http_*`` only ever in
the API process, so each has a single backend. Inference does not: ``chat`` and
``rerank`` are called from the API process while ``embed`` and ``vlm`` are called
from inside the indexing pipeline, and both must land under the same metric name
so one PromQL query covers the whole system.

**Exactly one backend per process, chosen once.** Recording to both would
double-count every call: the API process initialises Ray, so ``ray.util.metrics``
there is live rather than a no-op, and the same event would appear on ``/metrics``
*and* on the node's Ray metrics agent. The rule is "record where this process can
actually be scraped":

* Inside a Ray actor — an indexing worker, or the API itself when
  ``ENABLE_RAY_SERVE=true`` makes it a Serve replica — use ``ray.util.metrics``.
  Serve replicas are not individually addressable over HTTP, so a
  ``prometheus_client`` counter in one replica is invisible to a scrape the
  Serve proxy routes to another.
* Otherwise (the plain uvicorn API of the standalone compose deployment) use
  ``prometheus_client``, served by ``/metrics``.

The check mirrors ``services/workers/task_state._task_state_storage_available``,
which already uses actor identity for the same kind of decision.

*Deployment consequence for S3-3 / S3-6 / S3-7:* under Ray Serve these series
appear on the Ray metrics target, not on ``/metrics``. A deployment that scrapes
only ``/metrics`` sees HTTP metrics and no inference metrics; both targets must
be scraped for the set to be complete.

**On the ``provider`` label.** It is the admin-configured endpoint *name*,
stamped onto each client by ``di/factories.make_component_factory`` — bounded by
configuration. Deliberately neither the model nor the base URL: both are
client-controllable through ``metadata.llm_override``, which would make the
label unbounded from the *value* side, the one cardinality failure
``FORBIDDEN_LABELS`` cannot catch.

A request that overrides the endpoint is attributed to the fixed bucket
``client_override``, so a third-party endpoint's failures cannot corrupt the
error rate ``OpenRagInferenceProviderDown`` alerts on. ``_circuit_breaker``
already draws that line with ``skip_if=_targets_client_endpoint``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple, Protocol

from core.observability._reporting import report_once
from core.observability.metric_specs import (
    CIRCUIT_BREAKER_STATE,
    INFERENCE_DURATION_SECONDS,
    INFERENCE_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
    MetricSpec,
)

#: Attribute ``make_component_factory`` stamps the endpoint name onto, read back
#: by ``services/inference/_metrics.resolve_provider``. Shared so the two sides
#: cannot drift apart on a typo.
PROVIDER_NAME_ATTR = "openrag_provider_name"

#: Fixed bucket for a call that targeted a client-supplied endpoint — a single
#: constant, never the override's URL.
CLIENT_OVERRIDE_PROVIDER = "client_override"


class _Instrument(Protocol):
    """The two backends' APIs differ; call sites should not have to know which."""

    def inc(self, value: float, tags: dict[str, str]) -> None: ...

    def observe(self, value: float, tags: dict[str, str]) -> None: ...

    def set(self, value: float, tags: dict[str, str]) -> None: ...


class _RayInstrument:
    def __init__(self, metric: object) -> None:
        self._metric = metric

    def inc(self, value: float, tags: dict[str, str]) -> None:
        self._metric.inc(value, tags=tags)

    def observe(self, value: float, tags: dict[str, str]) -> None:
        self._metric.observe(value, tags=tags)

    def set(self, value: float, tags: dict[str, str]) -> None:
        self._metric.set(value, tags=tags)


class _PrometheusInstrument:
    def __init__(self, metric: object) -> None:
        self._metric = metric

    def inc(self, value: float, tags: dict[str, str]) -> None:
        self._metric.labels(**tags).inc(value)

    def observe(self, value: float, tags: dict[str, str]) -> None:
        self._metric.labels(**tags).observe(value)

    def set(self, value: float, tags: dict[str, str]) -> None:
        self._metric.labels(**tags).set(value)


class _Instruments(NamedTuple):
    requests: _Instrument
    duration: _Instrument
    tokens: _Instrument
    circuit_breaker: _Instrument


@lru_cache(maxsize=1)
def _use_ray_backend() -> bool:
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


@lru_cache(maxsize=1)
def _instruments() -> _Instruments:
    if _use_ray_backend():
        from ray.util import metrics as ray_metrics

        def counter(spec: MetricSpec) -> _Instrument:
            return _RayInstrument(ray_metrics.Counter(spec.name, description=spec.description, tag_keys=spec.labels))

        def histogram(spec: MetricSpec) -> _Instrument:
            return _RayInstrument(
                ray_metrics.Histogram(
                    spec.name,
                    description=spec.description,
                    boundaries=list(spec.buckets),
                    tag_keys=spec.labels,
                )
            )

        def gauge(spec: MetricSpec) -> _Instrument:
            return _RayInstrument(ray_metrics.Gauge(spec.name, description=spec.description, tag_keys=spec.labels))
    else:
        import prometheus_client

        def counter(spec: MetricSpec) -> _Instrument:
            return _PrometheusInstrument(prometheus_client.Counter(spec.name, spec.description, list(spec.labels)))

        def histogram(spec: MetricSpec) -> _Instrument:
            return _PrometheusInstrument(
                prometheus_client.Histogram(spec.name, spec.description, list(spec.labels), buckets=spec.buckets)
            )

        def gauge(spec: MetricSpec) -> _Instrument:
            return _PrometheusInstrument(prometheus_client.Gauge(spec.name, spec.description, list(spec.labels)))

    return _Instruments(
        requests=counter(INFERENCE_REQUESTS_TOTAL),
        duration=histogram(INFERENCE_DURATION_SECONDS),
        tokens=counter(LLM_TOKENS_TOTAL),
        circuit_breaker=gauge(CIRCUIT_BREAKER_STATE),
    )


def record_inference(*, provider: str, operation: str, outcome: str, duration_seconds: float) -> None:
    """Record one completed call to an external inference endpoint.

    One observation per *logical* call, not per retry attempt: the decorator
    sits outside ``@with_retry``, so three transport retries that eventually
    succeed are one success. Per-attempt counts would make a flaky-but-
    recovering endpoint indistinguishable from a failing one.
    """
    try:
        instruments = _instruments()
        instruments.requests.inc(1, {"provider": provider, "operation": operation, "outcome": outcome})
        instruments.duration.observe(float(duration_seconds), {"provider": provider, "operation": operation})
    except Exception as exc:  # noqa: BLE001 - metrics must never fail a request
        report_once(INFERENCE_REQUESTS_TOTAL.name, exc)


def record_tokens(*, operation: str, prompt: int = 0, completion: int = 0) -> None:
    """Add to the aggregate token counters.

    Aggregate on purpose: "how much has this tenant consumed?" is a billing
    question answered from Postgres by D2, and answering it here would
    reintroduce ``partition`` through the back door.
    """
    try:
        tokens = _instruments().tokens
        for kind, value in (("prompt", prompt), ("completion", completion)):
            if value:
                tokens.inc(int(value), {"operation": operation, "kind": kind})
    except Exception as exc:  # noqa: BLE001
        report_once(LLM_TOKENS_TOTAL.name, exc)


def record_circuit_breaker_state(name: str, state: int) -> None:
    """Publish a breaker's state.

    Routed through this module rather than a module-level ``prometheus_client``
    Gauge because the breakers trip on both sides of Ray: the ``llm`` breaker in
    the API process serving chat, the ``embedder`` and ``vlm`` breakers inside
    indexing workers. The previous Gauge was only ever visible for the former,
    so a tripped embedder — the failure most worth alerting on, because it stops
    ingestion entirely — was invisible to every scrape.
    """
    try:
        _instruments().circuit_breaker.set(state, {"name": name})
    except Exception as exc:  # noqa: BLE001 - a breaker trip must still be logged
        report_once(CIRCUIT_BREAKER_STATE.name, exc)


def record_usage_from_response(response: object, *, operation: str) -> None:
    """Count an OpenAI-shaped ``usage`` block, if the response carries one.

    Read defensively and silently. ``usage`` is optional in the OpenAI schema,
    absent from some gateways entirely, and absent from every streaming response
    unless the request asked for ``stream_options.include_usage``. A provider
    that never reports it must degrade to "no token metric", not to an error on
    every call.
    """
    if not isinstance(response, dict):
        return
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return
    if not prompt and not completion:
        # A provider reporting nothing, not a call that consumed nothing.
        return
    record_tokens(operation=operation, prompt=prompt, completion=completion)


__all__ = [
    "CLIENT_OVERRIDE_PROVIDER",
    "PROVIDER_NAME_ATTR",
    "record_circuit_breaker_state",
    "record_inference",
    "record_tokens",
    "record_usage_from_response",
]
