"""Inference metrics — the one Tier-1 set produced on both sides of Ray.

``ingest_*`` metrics only ever happen in a worker and ``http_*`` only ever in
the API process, so each has a single backend. Inference does not: ``chat`` and
``rerank`` are called from the API process while ``embed`` and ``vlm`` are called
from inside the indexing pipeline, and both must land under the same metric name
so one PromQL query covers the whole system.

**Exactly one backend per process, chosen once.** Recording to both would
double-count every call: the API process initialises Ray, so ``ray.util.metrics``
there is live rather than a no-op, and the same event would appear on ``/metrics``
*and* on the node's Ray metrics agent. The rule is therefore "record where this
process can actually be scraped":

* Inside a Ray actor — an indexing worker, or the API itself when
  ``ENABLE_RAY_SERVE=true`` makes it a Serve replica — use ``ray.util.metrics``.
  Serve replicas are not individually addressable over HTTP, so a
  ``prometheus_client`` counter in one replica is invisible to a scrape that the
  Serve proxy routes to a different one.
* Otherwise (the plain uvicorn API of the standalone compose deployment) use
  ``prometheus_client``, served by ``/metrics``.

The check mirrors ``services/workers/task_state._task_state_storage_available``,
which already uses actor identity to decide whether it is running somewhere with
Ray's facilities available.

*Deployment consequence for S3-3 / S3-6 / S3-7:* under Ray Serve these series
appear on the Ray metrics target, not on ``/metrics``. A deployment that scrapes
only ``/metrics`` will see HTTP metrics and no inference metrics. Both targets
have to be scraped for the set to be complete.

**On the ``provider`` label.** It is the admin-configured endpoint *name* from
``di/factories.make_component_factory`` — bounded by configuration. It is
deliberately neither the model nor the base URL: both are client-controllable
through ``metadata.llm_override``, which would make the label unbounded from the
value side, the one way a metric can blow up cardinality that
``FORBIDDEN_LABELS`` cannot catch.

A request that overrides the endpoint is attributed to the fixed bucket
``client_override`` rather than to the operator's provider. Counting a
third-party endpoint's failures against our own would corrupt the error rate
``OpenRagInferenceProviderDown`` alerts on. ``_circuit_breaker`` already draws
this line — ``skip_if=_targets_client_endpoint`` keeps a client's endpoint from
tripping our breaker — and this follows it.
"""

from __future__ import annotations

from functools import lru_cache

from core.observability.metric_specs import (
    INFERENCE_DURATION_SECONDS,
    INFERENCE_REQUESTS_TOTAL,
    LLM_TOKENS_TOTAL,
)
from core.utils.logging import get_logger

logger = get_logger()

#: Fixed bucket for a call that targeted a client-supplied endpoint. A single
#: constant, never the override's URL — that would be unbounded.
CLIENT_OVERRIDE_PROVIDER = "client_override"

_warned = False


def _warn_once(exc: Exception, what: str) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(f"inference metric recording failed ({what}); further errors in this process are not logged: {exc}")


@lru_cache(maxsize=1)
def _use_ray_backend() -> bool:
    """Whether this process should export through Ray rather than ``/metrics``.

    Cached: a process does not migrate between the two, and this is called on
    every inference call. Any failure resolving Ray's context means we are not
    in an actor, so the ``prometheus_client`` path is the safe answer.
    """
    try:
        import ray

        return ray.get_runtime_context().get_actor_id() is not None
    except Exception:  # noqa: BLE001 - absence of Ray is a valid answer, not an error
        return False


@lru_cache(maxsize=1)
def _instruments() -> tuple:
    """Build the three instruments once, against the backend this process uses."""
    if _use_ray_backend():
        from ray.util.metrics import Counter, Histogram

        return (
            Counter(
                INFERENCE_REQUESTS_TOTAL.name,
                description=INFERENCE_REQUESTS_TOTAL.description,
                tag_keys=INFERENCE_REQUESTS_TOTAL.labels,
            ),
            Histogram(
                INFERENCE_DURATION_SECONDS.name,
                description=INFERENCE_DURATION_SECONDS.description,
                boundaries=list(INFERENCE_DURATION_SECONDS.buckets or ()),
                tag_keys=INFERENCE_DURATION_SECONDS.labels,
            ),
            Counter(
                LLM_TOKENS_TOTAL.name,
                description=LLM_TOKENS_TOTAL.description,
                tag_keys=LLM_TOKENS_TOTAL.labels,
            ),
            True,
        )

    from prometheus_client import Counter as PCounter
    from prometheus_client import Histogram as PHistogram

    return (
        PCounter(
            INFERENCE_REQUESTS_TOTAL.name,
            INFERENCE_REQUESTS_TOTAL.description,
            list(INFERENCE_REQUESTS_TOTAL.labels),
        ),
        PHistogram(
            INFERENCE_DURATION_SECONDS.name,
            INFERENCE_DURATION_SECONDS.description,
            list(INFERENCE_DURATION_SECONDS.labels),
            buckets=(*(INFERENCE_DURATION_SECONDS.buckets or ()), float("inf")),
        ),
        PCounter(LLM_TOKENS_TOTAL.name, LLM_TOKENS_TOTAL.description, list(LLM_TOKENS_TOTAL.labels)),
        False,
    )


def record_inference(*, provider: str, operation: str, outcome: str, duration_seconds: float) -> None:
    """Record one completed call to an external inference endpoint.

    One observation per *logical* call, not per retry attempt: the decorator
    sits outside ``@with_retry``, so three transport retries that eventually
    succeed are one success here. Per-attempt counts would make a flaky-but-
    recovering endpoint look like a failing one in the error ratio.
    """
    try:
        requests, duration, _tokens, is_ray = _instruments()
        tags = {"provider": provider, "operation": operation, "outcome": outcome}
        if is_ray:
            requests.inc(1, tags=tags)
            duration.observe(float(duration_seconds), tags={"provider": provider, "operation": operation})
        else:
            requests.labels(**tags).inc()
            duration.labels(provider=provider, operation=operation).observe(float(duration_seconds))
    except Exception as exc:  # noqa: BLE001 - metrics must never fail a request
        _warn_once(exc, "inference_requests_total")


def record_tokens(*, operation: str, prompt: int = 0, completion: int = 0) -> None:
    """Add to the aggregate token counters.

    Aggregate on purpose. "How much has this tenant consumed?" is a billing
    question answered from Postgres by D2; answering it here would reintroduce
    ``partition`` through the back door.
    """
    try:
        _requests, _duration, tokens, is_ray = _instruments()
        for kind, value in (("prompt", prompt), ("completion", completion)):
            if not value:
                continue
            if is_ray:
                tokens.inc(int(value), tags={"operation": operation, "kind": kind})
            else:
                tokens.labels(operation=operation, kind=kind).inc(int(value))
    except Exception as exc:  # noqa: BLE001
        _warn_once(exc, "llm_tokens_total")


def record_usage_from_response(response: object, *, operation: str) -> None:
    """Extract an OpenAI-shaped ``usage`` block from a response and count it.

    Read defensively and silently: ``usage`` is optional in the OpenAI schema,
    absent from some gateways entirely, and absent from *every* streaming
    response unless the request asked for ``stream_options.include_usage``.
    A provider that never reports usage must degrade to "no token metric", not
    to an error on every call.
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
        # An empty or all-zero usage block is a provider reporting nothing,
        # not a call that consumed nothing. Returning here keeps the
        # distinction visible to anyone reading the code path.
        return
    record_tokens(operation=operation, prompt=prompt, completion=completion)


__all__ = [
    "CLIENT_OVERRIDE_PROVIDER",
    "record_inference",
    "record_tokens",
    "record_usage_from_response",
]
