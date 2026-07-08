import httpx
from core.utils.exceptions import OpenRAGError
from core.utils.logging import get_logger
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = get_logger()

_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, OpenRAGError):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    return False


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    logger.warning(
        "Retrying after transient failure (attempt {attempt}/{max}): {exc}",
        attempt=state.attempt_number,
        max=state.retry_object.stop.max_attempt_number,  # type: ignore[union-attr]
        exc=repr(exc),
    )


def with_retry(max_attempts: int = 3, base_wait: float = 1.0, max_wait: float = 30.0):
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=base_wait, max=max_wait, exp_base=2),
        retry=retry_if_exception(_is_retryable),
        before_sleep=_log_retry,
        reraise=True,
    )
