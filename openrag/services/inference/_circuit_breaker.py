from collections.abc import Callable
from datetime import timedelta
from functools import wraps

import httpx
from aiobreaker import CircuitBreaker, CircuitBreakerError, CircuitBreakerListener
from aiobreaker.state import CircuitBreakerState
from core.observability.inference_metrics import record_circuit_breaker_state
from core.utils.exceptions import CircuitBreakerOpenError, LLMParsingError, OpenRAGError
from core.utils.logging import get_logger

logger = get_logger()

_breakers: dict[str, CircuitBreaker] = {}
_breaker_config: dict[str, tuple[int, float]] = {}

#: Keyed on aiobreaker's own enum, not on ``type(state).__name__``. The class
#: names are ``CircuitOpenState``/``CircuitClosedState``/``CircuitHalfOpenState``;
#: keying on ``"OpenState"`` matched none of them, so every transition recorded
#: ``_UNKNOWN_STATE`` and ``openrag_circuit_breaker_state`` was permanently -1 —
#: which makes ``OpenRagCircuitBreakerOpen`` (``== 1``) unable to fire. Using the
#: enum means a library rename breaks the import loudly instead.
_STATE_VALUES = {
    CircuitBreakerState.CLOSED: 0,
    CircuitBreakerState.OPEN: 1,
    CircuitBreakerState.HALF_OPEN: 2,
}
_UNKNOWN_STATE = -1


#: 4xx statuses that say the provider will not serve *us* — a revoked, expired
#: or wrong credential — rather than that one request was bad. The metrics
#: count them as failures of that endpoint (``_metrics.outcome_for``): every
#: call fails the same way until an operator fixes the key, and
#: ``OpenRagInferenceProviderDown`` then names the endpoint.
#:
#: 401 only. 403 can also mean "this key may not use *that* model".
#: OpenAI-compatible APIs answer a bad key with 401.
PROVIDER_AUTH_4XX = frozenset({401})


def counts_refused_credential(status: int, *, caller_shaped: bool) -> bool:
    """Is *status* our credential being refused, rather than this request?

    For the metrics only. ``caller_shaped`` is true for LLM calls: callers pick
    the model (``llm_override``) and extra chat-body fields are forwarded, so a
    401 there can be the caller's doing (LiteLLM answers a refused model, or an
    ``api_key`` sent in the body, with 401)."""
    return status in PROVIDER_AUTH_4XX and not caller_shaped


def _is_client_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, OpenRAGError):
        status = exc.status_code
    else:
        return False
    return 400 <= status < 500


def _is_excluded(exc: Exception) -> bool:
    """No 4xx opens a breaker, 401 included. Breakers are shared per kind, not
    per endpoint (``get_breaker(name)``), so one endpoint's bad key counted here
    opened the ``embedder`` breaker for every partition (#1100). The key is
    still the endpoint's failure in the metrics, where it is labelled by
    endpoint and does not stop the others."""
    if _is_client_error(exc):
        return True
    if isinstance(exc, LLMParsingError):
        return True
    return False


class _LoggingListener(CircuitBreakerListener):
    def state_change(self, breaker, old, new):
        state_name = type(new).__name__
        logger.warning(
            "Circuit breaker '{name}' state: {old} -> {new}",
            name=breaker.name,
            old=type(old).__name__,
            new=state_name,
        )
        record_circuit_breaker_state(breaker.name, _STATE_VALUES.get(new.state, _UNKNOWN_STATE))


def get_breaker(name: str, fail_max: int = 50, timeout_duration: float = 60.0) -> CircuitBreaker:
    requested = (fail_max, timeout_duration)
    if name not in _breakers:
        breaker = CircuitBreaker(
            fail_max=fail_max,
            timeout_duration=timedelta(seconds=timeout_duration),
            name=name,
            exclude=[_is_excluded],
            listeners=[_LoggingListener()],
        )
        # State is otherwise written only on a transition, so a breaker that
        # never tripped had no series: "Unknown" on a healthy system. Written
        # before the breaker is registered: once a caller can reach it, a
        # transition may already have exported "open", which this would undo.
        record_circuit_breaker_state(name, _STATE_VALUES[CircuitBreakerState.CLOSED])
        _breakers[name] = breaker
        _breaker_config[name] = requested
    elif _breaker_config.get(name) != requested:
        raise ValueError(f"Breaker '{name}' already exists with config={_breaker_config[name]}, requested={requested}")
    return _breakers[name]


def with_circuit_breaker(
    name: str,
    fail_max: int = 50,
    timeout_duration: float = 60.0,
    *,
    skip_if: Callable[..., bool] | None = None,
):
    """Guard *fn* with the shared breaker registered under *name*.

    *skip_if* receives the wrapped call's own arguments; returning True runs *fn*
    outside the breaker entirely. For calls that don't reach the endpoint this
    breaker describes — folding a second dependency into one health signal makes
    it wrong in both directions.
    """

    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if skip_if is not None and skip_if(*args, **kwargs):
                return await fn(*args, **kwargs)
            breaker = get_breaker(name, fail_max, timeout_duration)
            try:
                return await breaker.call_async(fn, *args, **kwargs)
            except CircuitBreakerError as exc:
                # A dedicated type so the metrics decorator wrapping this one can
                # tell an open circuit from a connection failure: it sat outside
                # this decorator and only ever saw the converted error, which made
                # outcome="circuit_open" unreachable and counted open-circuit calls
                # as errors — holding a provider's error ratio high for as long as
                # the breaker protected the endpoint.
                #
                # This is a 503 where the old InferenceConnectionError was not.
                # Nothing in the repository catches that type, and "we stopped
                # calling the endpoint" is service-unavailable rather than a
                # connection fault, so the status is the more accurate one.
                raise CircuitBreakerOpenError(name) from exc

        return wrapper

    return decorator
