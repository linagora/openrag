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
from core.utils.exceptions import (
    CircuitBreakerOpenError,
    EmbeddingTimeoutError,
    InferenceError,
    InferenceTimeoutError,
)
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
    error", for the dashboard and for the alert.

    Driven through the real decorator stack rather than by raising aiobreaker's
    error by hand: ``with_circuit_breaker`` converts that one, so a test that
    raises it directly passes while production records every open circuit as a
    plain ``error``. The first version of this test did exactly that, and the
    bug it was meant to guard shipped underneath it.
    """
    from services.inference._circuit_breaker import with_circuit_breaker

    class Svc(_Client):
        @with_inference_metrics("chat")
        @with_circuit_breaker("test-open-outcome", fail_max=2, timeout_duration=60.0)
        async def call(self) -> None:
            raise ConnectionError("endpoint down")

    svc = Svc()
    # Propagates and trips the breaker (fail_max=2 means the *next* call is the
    # one aiobreaker refuses); recorded as a plain error, which it is.
    with pytest.raises(ConnectionError):
        await svc.call()
    # Open now, so this one never reaches the endpoint.
    with pytest.raises(CircuitBreakerOpenError):
        await svc.call()

    outcomes = [c["outcome"] for c in recorded["inference"]]
    assert "circuit_open" in outcomes, f"an open circuit was not recorded as such: {outcomes}"


async def test_embedding_timeouts_are_counted_as_timeouts(recorded) -> None:
    """``EmbeddingTimeoutError`` descends from ``EmbeddingError``, not from
    ``InferenceTimeoutError``, so a vLLM embedding timeout used to land in the
    generic ``error`` bucket — leaving the timeout ratio reading low exactly
    where embedding capacity was the problem."""

    class Svc(_Client):
        @with_inference_metrics("embed")
        async def call(self) -> None:
            raise EmbeddingTimeoutError("embedder timed out")

    with pytest.raises(EmbeddingTimeoutError):
        await Svc().call()

    assert [c["outcome"] for c in recorded["inference"]] == ["timeout"]


@pytest.mark.asyncio
async def test_cancelled_request_is_neither_a_success_nor_a_provider_error(recorded) -> None:
    """A client disconnecting mid-answer has not received a completion, so it
    is not a success — but the provider did nothing wrong, so it must stay out
    of the error ratio the provider alerts threshold on."""
    import asyncio

    class Svc(_Client):
        @with_inference_metrics("chat")
        async def call(self) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await Svc().call()

    assert recorded["inference"][0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_a_callers_deadline_is_recorded_as_cancelled(recorded) -> None:
    """The indexing stages bound calls with ``asyncio.wait_for``, which
    cancels the call itself — the production path a slow provider takes."""
    import asyncio

    class Svc(_Client):
        @with_inference_metrics("embed")
        async def call(self) -> None:
            await asyncio.sleep(10)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(Svc().call(), timeout=0.01)

    assert [c["outcome"] for c in recorded["inference"]] == ["cancelled"]


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


_USAGE_CHUNK = 'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}'
_USAGE_STREAM = ['data: {"choices":[{"delta":{"content":"hi"}}]}', _USAGE_CHUNK, "data: [DONE]"]


@pytest.mark.asyncio
async def test_stream_chat_requests_usage_and_withholds_it_from_the_caller(
    monkeypatch: pytest.MonkeyPatch, recorded
) -> None:
    """Without ``stream_options.include_usage`` a streamed answer carries no
    usage block at all, so every chat — the primary user-facing path —
    contributes zero to the cost metric while looking instrumented. Asked for
    the metric only, the usage chunk must not reach a client that never
    requested it."""
    client = _streaming_client(_USAGE_STREAM, monkeypatch, [])

    lines = [line async for line in client.stream_chat([{"role": "user", "content": "q"}])]

    assert client._client.bodies[0]["stream_options"] == {"include_usage": True}
    assert _USAGE_CHUNK not in lines
    assert recorded["tokens"] == [{"operation": "chat", "prompt": 7, "completion": 3}]


@pytest.mark.asyncio
async def test_stream_chat_keeps_the_callers_own_stream_options(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _streaming_client(_USAGE_STREAM, monkeypatch, [])

    lines = [
        line
        async for line in client.stream_chat(
            [{"role": "user", "content": "q"}],
            stream_options={"include_usage": True, "continuous_usage_stats": True},
        )
    ]

    assert client._client.bodies[0]["stream_options"] == {"include_usage": True, "continuous_usage_stats": True}
    assert _USAGE_CHUNK in lines


@pytest.mark.asyncio
async def test_stream_chat_adds_nothing_for_a_client_supplied_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another provider may reject the unknown field, and override traffic is
    labelled ``client_override`` anyway."""
    client = _streaming_client(["data: [DONE]"], monkeypatch, [])
    client._allow_custom_endpoint = True  # LLM_OVERRIDE_ALLOW_CUSTOM_ENDPOINT; ignored otherwise
    metadata = {"llm_override": {"base_url": "https://other.example/v1", "model": "m", "api_key": "k"}}

    [line async for line in client.stream_chat([{"role": "user", "content": "q"}], metadata=metadata)]

    assert "stream_options" not in client._client.bodies[0]


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


# ---------------------------------------------------------------------------
# Regression: the consumer closes this generator, and that is not a failure
# ---------------------------------------------------------------------------
# ``stream_with_source_filtering`` breaks out of its loop on ``data: [DONE]``
# and then closes the iterator (source_filtering.py:173 and the aclose() that
# follows it). Closing raises GeneratorExit inside ``stream_chat`` at the yield.
# Treating that as a failure reported *every completed chat* as an error — the
# error-rate panel would have read ~100% while the system worked perfectly.


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class _FakeHttpClient:
    def __init__(self, lines: list[str], status_code: int = 200) -> None:
        self._lines = lines
        self._status_code = status_code
        self.bodies: list[dict] = []

    def stream(self, *_args: Any, **kwargs: Any) -> _FakeStreamContext:
        self.bodies.append(kwargs.get("json") or {})
        return _FakeStreamContext(_FakeStreamResponse(self._lines, self._status_code))


def _streaming_client(lines: list[str], monkeypatch: pytest.MonkeyPatch, calls: list) -> Any:
    import services.inference.vllm_client as vc

    monkeypatch.setattr(vc, "record_inference", lambda **kw: calls.append(kw))
    client = vc.VLLMClient("http://llm.invalid/v1", "test-model")
    client._client = _FakeHttpClient(lines)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "outcome"), [(400, "rejected"), (429, "error"), (503, "error")])
async def test_a_refused_stream_is_classified_by_status(
    monkeypatch: pytest.MonkeyPatch, status: int, outcome: str
) -> None:
    from core.utils.exceptions import InferenceError

    calls: list = []
    client = _streaming_client([], monkeypatch, calls)
    client._client = _FakeHttpClient([], status_code=status)

    with pytest.raises(InferenceError):
        async for _ in client.stream_chat([{"role": "user", "content": "q"}]):
            pass

    assert [c["outcome"] for c in calls] == [outcome]


@pytest.mark.asyncio
async def test_stream_closed_after_done_is_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces the real consumer: break on ``[DONE]``, then close."""
    calls: list = []
    client = _streaming_client(
        ['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"],
        monkeypatch,
        calls,
    )

    stream = client.stream_chat([{"role": "user", "content": "q"}])
    async for line in stream:
        if line.strip() == "data: [DONE]":
            break
    await stream.aclose()

    assert [c["outcome"] for c in calls] == ["success"]


@pytest.mark.asyncio
async def test_stream_abandoned_before_done_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that gives up mid-answer did not get a completion, but the
    provider did not fail either: neither a success nor an error."""
    calls: list = []
    client = _streaming_client(
        [f'data: {{"choices":[{{"delta":{{"content":"{i}"}}}}]}}' for i in range(5)] + ["data: [DONE]"],
        monkeypatch,
        calls,
    )

    stream = client.stream_chat([{"role": "user", "content": "q"}])
    async for _line in stream:
        break
    await stream.aclose()

    assert [c["outcome"] for c in calls] == ["cancelled"]


@pytest.mark.asyncio
async def test_stream_truncated_without_done_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """An upstream that ends without ``[DONE]`` truncated the answer —
    ``source_filtering`` reports exactly that to the caller, so the metric must
    not disagree with the message the user receives."""
    calls: list = []
    client = _streaming_client(['data: {"choices":[{"delta":{"content":"hi"}}]}'], monkeypatch, calls)

    stream = client.stream_chat([{"role": "user", "content": "q"}])
    async for _line in stream:
        pass

    assert [c["outcome"] for c in calls] == ["error"]


def test_content_delta_is_not_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must reject ordinary deltas before ``json.loads``.

    This runs for every SSE line — hundreds per answer — and the consumer
    already parses each one downstream.
    """
    import services.inference.vllm_client as vc

    parsed: list[str] = []
    monkeypatch.setattr(vc.json, "loads", lambda text: parsed.append(text) or {})

    vc._record_stream_usage('data: {"choices":[{"delta":{"content":"hello"}}]}')

    assert parsed == []


def test_content_mentioning_usage_records_nothing(recorded) -> None:
    """A model answering a question *about* usage trips the cheap substring
    guard; ``record_usage_from_response`` must still reject it, because ``usage``
    is not a top-level object there."""
    import services.inference.vllm_client as vc

    vc._record_stream_usage('data: {"choices":[{"delta":{"content":"your \\"usage\\" is high"}}]}')

    assert recorded["tokens"] == []


# ---------------------------------------------------------------------------
# The declared label domains must be the ones the code actually emits
# ---------------------------------------------------------------------------
# Without these, INFERENCE_OUTCOME_VALUES and TOKEN_KIND_VALUES are comments
# that happen to be typed as tuples: nothing would notice a new outcome string
# appearing in outcome_for, and S3-4's alert expressions are written against
# the declared set.


@pytest.mark.parametrize(
    "exc",
    [
        InferenceTimeoutError("slow"),
        InferenceError("bad gateway"),
        InferenceError("unknown model", status_code=404),
        ValueError("unexpected"),
        KeyboardInterrupt(),
    ],
)
def test_every_outcome_is_a_declared_value(exc: BaseException) -> None:
    from core.observability.metric_specs import INFERENCE_OUTCOME_VALUES
    from services.inference._metrics import outcome_for

    assert outcome_for(exc) in INFERENCE_OUTCOME_VALUES


def test_success_is_a_declared_outcome() -> None:
    """``with_inference_metrics`` writes this one directly rather than through
    ``outcome_for``, so it needs its own assertion."""
    from core.observability.metric_specs import INFERENCE_OUTCOME_VALUES

    assert "success" in INFERENCE_OUTCOME_VALUES


def test_token_kinds_are_declared_values(recorded) -> None:
    from core.observability.metric_specs import TOKEN_KIND_VALUES

    inference_metrics.record_usage_from_response(
        {"usage": {"prompt_tokens": 5, "completion_tokens": 7}}, operation="chat"
    )

    emitted = {k for call in recorded["tokens"] for k in ("prompt", "completion") if call.get(k)}
    assert emitted <= set(TOKEN_KIND_VALUES)
