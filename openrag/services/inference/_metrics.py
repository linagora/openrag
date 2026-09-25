"""``@with_inference_metrics`` — the decorator that instruments an endpoint call.

Sits alongside ``_circuit_breaker`` and ``_retry`` in the same decorator stack,
and its **position in that stack is part of the contract**::

    @with_inference_metrics("chat")      # outermost
    @with_circuit_breaker("llm", ...)
    @with_retry(max_attempts=3)
    async def chat(self, ...): ...

Outermost, for two reasons.

*One observation per logical call.* Inside ``@with_retry`` every transport
attempt would be counted, so an endpoint that is flaky but recovering would
present in the error ratio identically to one that is down. The question the
metric answers is "did this call succeed", not "how many packets did it take".
The duration recorded is likewise the whole call, retries included, which is
what a caller actually waited.

*``circuit_open`` is visible.* ``CircuitBreakerError`` is raised *by*
``with_circuit_breaker``, so anything underneath it never sees the open-circuit
case — and "we stopped even trying" is a materially different condition from "it
returned an error", both for the dashboard and for the S3-4 alert.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from functools import wraps
from typing import Any

import httpx
from core.observability.inference_metrics import (
    CLIENT_OVERRIDE_PROVIDER,
    INFERENCE_OPERATION_ATTR,
    PROVIDER_NAME_ATTR,
    record_inference,
    record_usage_from_response,
)
from core.utils.exceptions import (
    CircuitBreakerOpenError,
    EmbeddingTimeoutError,
    InferenceTimeoutError,
    OpenRAGError,
)
from services.inference._circuit_breaker import refuses_our_credential

#: Fallback for a client built outside the factory (tests, scripts): it still
#: records, and lands in a fixed bucket instead of minting a label value.
_UNKNOWN_PROVIDER = "unconfigured"


def resolve_provider(instance: Any, kwargs: dict[str, Any]) -> str:
    """The bounded ``provider`` label for a call.

    Never the model or the base URL — both are client-controllable through
    ``metadata.llm_override``, and a label whose values callers choose is the
    one cardinality failure ``FORBIDDEN_LABELS`` cannot catch, because it is the
    values that are unbounded rather than the keys.
    """
    has_override = getattr(instance, "_has_endpoint_override", None)
    if callable(has_override):
        try:
            if has_override(kwargs):
                return CLIENT_OVERRIDE_PROVIDER
        except Exception:  # noqa: BLE001 - never let label resolution fail a call
            pass
    name = getattr(instance, PROVIDER_NAME_ATTR, None)
    return name if isinstance(name, str) and name else _UNKNOWN_PROVIDER


def outcome_for(exc: BaseException) -> str:
    """Classify a failed call into one of ``INFERENCE_OUTCOME_VALUES``.

    Both checks are on *families*, deliberately.

    ``CircuitBreakerOpenError`` rather than aiobreaker's ``CircuitBreakerError``:
    ``with_circuit_breaker`` sits inside this decorator and converts the latter,
    so matching on it recorded every open circuit as a plain ``error``.

    Embedding clients raise ``EmbeddingTimeoutError``, which descends from
    ``EmbeddingError`` and not from ``InferenceTimeoutError`` — so a vLLM
    embedding timeout landed in the generic bucket and left the timeout ratio
    reading low exactly where embedding capacity was the problem.

    Cancellation is the caller giving up, not the provider failing: a client
    stopping a stream, a caller's ``asyncio.wait_for`` deadline, or the sibling
    batches cancelled after one failed. Counting it as ``error`` let one real
    failure — or users pressing stop — raise a healthy provider's error ratio.

    A 4xx is the provider refusing *this request* — an unknown model named in
    ``metadata.llm_override``, a prompt over the context length — so any user
    could otherwise drive a healthy provider's error ratio up at will. Throttling
    (429) and a provider-side request timeout (408) are the provider struggling,
    and stay ``error``; so does 401, a credential the provider refuses, which
    fails every call alike — unless the caller chose the model or endpoint, when
    it can be that model refused (``refuses_our_credential``). 403 stays
    ``rejected``: it can be one request's model the key may not use. The circuit
    breaker counts 401 alike, but still excludes 408 and 429 (``_is_excluded``).
    """
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    if isinstance(exc, CircuitBreakerOpenError):
        return "circuit_open"
    if isinstance(exc, (InferenceTimeoutError, EmbeddingTimeoutError)):
        return "timeout"
    if _is_rejected_request(exc):
        return "rejected"
    return "error"


#: 4xx statuses that describe the provider's state, not the request's.
_PROVIDER_SIDE_4XX = frozenset({408, 429})


def _is_rejected_request(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, OpenRAGError):
        status = exc.status_code
    else:
        return False
    return 400 <= status < 500 and status not in _PROVIDER_SIDE_4XX and not refuses_our_credential(exc, status)


def with_inference_metrics(operation: str, *, capture_usage: bool = False) -> Callable:
    """Instrument one endpoint-calling coroutine method.

    Args:
        operation: One of ``INFERENCE_OPERATION_VALUES`` — a *kind* of call, not
            a model name.
        capture_usage: Read an OpenAI-shaped ``usage`` block off the returned
            payload into ``openrag_llm_tokens_total``. Only for methods that
            return the raw provider response; embedding and rerank responses
            carry no usage.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            provider = resolve_provider(self, kwargs)
            start = time.perf_counter()
            outcome = "success"
            try:
                result = await func(self, *args, **kwargs)
            except BaseException as exc:
                # BaseException so a cancelled request is not silently recorded
                # as a success; CancelledError is recorded as "cancelled" and
                # re-raised untouched.
                outcome = outcome_for(exc)
                raise
            finally:
                record_inference(
                    provider=provider,
                    operation=operation,
                    outcome=outcome,
                    duration_seconds=time.perf_counter() - start,
                )
            if capture_usage:
                record_usage_from_response(result, operation=operation)
            return result

        # Read by ``set_provider_name`` to start this client's series at 0.
        setattr(wrapper, INFERENCE_OPERATION_ATTR, operation)
        return wrapper

    return decorator


__all__ = ["outcome_for", "resolve_provider", "with_inference_metrics"]
