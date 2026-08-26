from collections.abc import Callable
from datetime import timedelta
from functools import wraps

import httpx
from aiobreaker import CircuitBreaker, CircuitBreakerError, CircuitBreakerListener
from core.utils.exceptions import InferenceConnectionError, LLMParsingError, OpenRAGError
from core.utils.logging import get_logger
from prometheus_client import Gauge

logger = get_logger()

_breakers: dict[str, CircuitBreaker] = {}
_breaker_config: dict[str, tuple[int, float]] = {}

try:
    CIRCUIT_BREAKER_STATE = Gauge(
        "openrag_circuit_breaker_state",
        "Circuit breaker state (0=closed, 1=open, 2=half-open)",
        ["name"],
    )
except ValueError:
    from prometheus_client import REGISTRY

    CIRCUIT_BREAKER_STATE = REGISTRY._names_to_collectors["openrag_circuit_breaker_state"]

_STATE_VALUES = {"ClosedState": 0, "OpenState": 1, "HalfOpenState": 2}


def _is_client_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return 400 <= exc.response.status_code < 500
    if isinstance(exc, OpenRAGError):
        return 400 <= exc.status_code < 500
    return False


def _is_excluded(exc: Exception) -> bool:
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
        CIRCUIT_BREAKER_STATE.labels(name=breaker.name).set(_STATE_VALUES.get(state_name, -1))


def get_breaker(name: str, fail_max: int = 50, timeout_duration: float = 60.0) -> CircuitBreaker:
    requested = (fail_max, timeout_duration)
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(
            fail_max=fail_max,
            timeout_duration=timedelta(seconds=timeout_duration),
            name=name,
            exclude=[_is_excluded],
            listeners=[_LoggingListener()],
        )
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
            except CircuitBreakerError:
                raise InferenceConnectionError(f"Circuit open for '{name}'")

        return wrapper

    return decorator
