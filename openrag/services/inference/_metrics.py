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

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from aiobreaker import CircuitBreakerError
from core.observability.inference_metrics import (
    CLIENT_OVERRIDE_PROVIDER,
    record_inference,
    record_usage_from_response,
)
from core.utils.exceptions import InferenceTimeoutError

#: Set on each client instance by ``di/factories.make_component_factory``. The
#: fallback matters: a client built directly (tests, scripts, a code path that
#: bypasses the factory) still records rather than raising, and lands in a fixed
#: bucket instead of minting a label value.
PROVIDER_NAME_ATTR = "openrag_provider_name"
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


def _outcome_for(exc: BaseException) -> str:
    if isinstance(exc, CircuitBreakerError):
        return "circuit_open"
    if isinstance(exc, InferenceTimeoutError):
        return "timeout"
    return "error"


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
                # as a success; CancelledError falls through to "error" and is
                # re-raised untouched.
                outcome = _outcome_for(exc)
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

        return wrapper

    return decorator


__all__ = ["PROVIDER_NAME_ATTR", "resolve_provider", "with_inference_metrics"]
