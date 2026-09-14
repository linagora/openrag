"""S3-2 Phase 3 — inference metrics, token capture, and the provider label.

The three things that can go wrong here are all invisible at runtime:

* **The provider label becomes unbounded.** ``model`` and ``base_url`` are both
  client-controllable via ``metadata.llm_override``. Labelling by either would
  let a caller mint Prometheus series — the one cardinality failure
  ``FORBIDDEN_LABELS`` cannot catch, because there the *values* are unbounded
  rather than the keys.
* **The decorator drifts down the stack.** Below ``@with_retry`` it counts
  attempts instead of calls; below ``@with_circuit_breaker`` it can never
  observe ``circuit_open``. Neither raises; both just make the error ratio lie.
* **Token capture silently measures nothing.** A streamed completion carries no
  ``usage`` unless the request asked for it, so the primary user-facing path
  contributes zero tokens while still looking instrumented.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from core.observability import inference_metrics
from core.utils.exceptions import InferenceError, InferenceTimeoutError
from services.inference._metrics import (
    PROVIDER_NAME_ATTR,
    resolve_provider,
    with_inference_metrics,
)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture what the metric layer was asked to record."""
    calls: dict[str, list] = {"inference": [], "tokens": []}
    monkeypatch.setattr(
        inference_metrics,
        "record_inference",
        lambda **kw: calls["inference"].append(kw),
    )
    monkeypatch.setattr(
        inference_metrics,
        "record_tokens",
        lambda **kw: calls["tokens"].append(kw),
    )
    # The decorator imported the names directly, so patch there too.
    import services.inference._metrics as metrics_module

    monkeypatch.setattr(metrics_module, "record_inference", lambda **kw: calls["inference"].append(kw))
    monkeypatch.setattr(
        metrics_module,
        "record_usage_from_response",
        lambda response, operation: inference_metrics.record_usage_from_response(response, operation=operation),
    )
    return calls


# ---------------------------------------------------------------------------
# The provider label
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, *, name: str | None = "default", overridden: bool = False) -> None:
        if name is not None:
            setattr(self, PROVIDER_NAME_ATTR, name)
        self._overridden = overridden

    def _has_endpoint_override(self, kwargs: dict[str, Any]) -> bool:
        return self._overridden


def test_provider_is_the_configured_endpoint_name() -> None:
    assert resolve_provider(_Client(name="large-context"), {}) == "large-context"


def test_client_supplied_endpoint_gets_a_fixed_bucket() -> None:
    """A request that overrides the endpoint is not attributed to the operator's
    provider.

    Two reasons, and the second is the important one. The override URL would be
    an unbounded label value; and counting a third-party endpoint's failures
    against our own would corrupt the error rate OpenRagInferenceProviderDown
    alerts on. ``_circuit_breaker`` already draws this line with
    ``skip_if=_targets_client_endpoint``.
    """
    assert resolve_provider(_Client(overridden=True), {}) == inference_metrics.CLIENT_OVERRIDE_PROVIDER


def test_unstamped_client_falls_back_to_a_constant() -> None:
    """A client built outside the factory (tests, scripts) still records, and
    lands in a fixed bucket rather than minting a value."""
    assert resolve_provider(_Client(name=None), {}) == "unconfigured"


def test_override_check_failure_does_not_break_the_call() -> None:
    class _Broken(_Client):
        def _has_endpoint_override(self, kwargs: dict[str, Any]) -> bool:
            raise RuntimeError("boom")

    assert resolve_provider(_Broken(name="default"), {}) == "default"


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_call_is_recorded_with_duration(recorded) -> None:
    class Svc(_Client):
        @with_inference_metrics("chat")
        async def call(self) -> str:
            return "ok"

    assert await Svc().call() == "ok"

    (entry,) = recorded["inference"]
    assert entry["operation"] == "chat"
    assert entry["outcome"] == "success"
    assert entry["provider"] == "default"
    assert entry["duration_seconds"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (InferenceTimeoutError("slow"), "timeout"),
        (InferenceError("bad gateway"), "error"),
        (ValueError("unexpected"), "error"),
    ],
)
async def test_failure_outcomes(recorded, exc: Exception, expected: str) -> None:
    class Svc(_Client):
        @with_inference_metrics("embed")
        async def call(self) -> None:
            raise exc

    with pytest.raises(type(exc)):
        await Svc().call()

    assert recorded["inference"][0]["outcome"] == expected


@pytest.mark.asyncio
async def test_open_circuit_is_its_own_outcome(recorded) -> None:
    """ "We stopped even trying" is a different condition from "it returned an
    error", for the dashboard and for the alert. Seeing it at all requires the
    decorator to sit *outside* ``@with_circuit_breaker``, which raises it.
    """
    from datetime import timedelta

    from aiobreaker import CircuitBreakerError

    class Svc(_Client):
        @with_inference_metrics("chat")
        async def call(self) -> None:
            raise CircuitBreakerError("open", timedelta(seconds=30))

    with pytest.raises(CircuitBreakerError):
        await Svc().call()

    assert recorded["inference"][0]["outcome"] == "circuit_open"


@pytest.mark.asyncio
async def test_cancelled_request_is_not_recorded_as_success(recorded) -> None:
    """A client disconnecting mid-answer has not received a completion.
    Recording it as success would understate the error rate exactly when users
    are giving up."""
    import asyncio

    class Svc(_Client):
        @with_inference_metrics("chat")
        async def call(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Svc().call()

    assert recorded["inference"][0]["outcome"] == "error"


# ---------------------------------------------------------------------------
# Token capture
# ---------------------------------------------------------------------------


def test_usage_block_is_counted(recorded) -> None:
    inference_metrics.record_usage_from_response(
        {"usage": {"prompt_tokens": 1200, "completion_tokens": 300}},
        operation="chat",
    )

    assert recorded["tokens"] == [{"operation": "chat", "prompt": 1200, "completion": 300}]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"usage": None},
        {"usage": {}},
        {"usage": {"prompt_tokens": "1200"}},
        "not a dict",
        None,
    ],
)
def test_missing_or_malformed_usage_is_ignored(recorded, response: Any) -> None:
    """``usage`` is optional in the OpenAI schema and absent from some gateways.
    A provider that never reports it must degrade to "no token metric", not to
    an error on every call."""
    inference_metrics.record_usage_from_response(response, operation="chat")

    assert recorded["tokens"] == []


# ---------------------------------------------------------------------------
# The streaming path — the one that would otherwise measure nothing
# ---------------------------------------------------------------------------


def test_stream_chat_requests_usage() -> None:
    """Without ``stream_options.include_usage`` a streamed answer carries no
    usage block at all, so every chat — the primary user-facing path —
    contributes zero to the cost metric while looking instrumented.
    """
    import inspect

    import services.inference.vllm_client as vc

    source = inspect.getsource(vc.VLLMClient.stream_chat)
    assert '"stream_options": {"include_usage": True}' in source


def test_stream_usage_chunk_is_counted(recorded) -> None:
    import services.inference.vllm_client as vc

    chunk = json.dumps({"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 7}})
    vc._record_stream_usage(f"data: {chunk}")

    assert recorded["tokens"] == [{"operation": "chat", "prompt": 40, "completion": 7}]


@pytest.mark.parametrize(
    "line",
    [
        "data: [DONE]",
        "",
        ": keepalive",
        "data: {not json",
        'data: {"choices": [{"delta": {"content": "hi"}}]}',
    ],
)
def test_ordinary_stream_lines_record_nothing(recorded, line: str) -> None:
    """Called once per SSE line — hundreds per answer — so every non-usage line
    must be cheap and silent."""
    import services.inference.vllm_client as vc

    vc._record_stream_usage(line)

    assert recorded["tokens"] == []


# ---------------------------------------------------------------------------
# Backend routing — exactly one per process
# ---------------------------------------------------------------------------


def test_backend_choice_is_cached_and_defaults_to_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recording to both backends would double-count: the API process
    initialises Ray, so ray.util.metrics there is live rather than a no-op and
    the same call would appear on /metrics *and* on Ray's metrics agent.

    Outside a Ray actor the answer must be prometheus_client, including when
    resolving Ray's context raises.
    """
    inference_metrics._use_ray_backend.cache_clear()

    import ray

    def _boom() -> Any:
        raise RuntimeError("no ray context")

    monkeypatch.setattr(ray, "get_runtime_context", _boom)
    assert inference_metrics._use_ray_backend() is False

    inference_metrics._use_ray_backend.cache_clear()
