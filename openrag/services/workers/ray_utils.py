"""Ray-actor concurrency helpers.

Two pairs of utilities, each in function and decorator form:

- timeout: ``call_ray_actor_with_timeout`` / ``@with_timeout``. Awaits a
  ``ray.ObjectRef`` with proper cancel-on-timeout semantics.
- retry:   ``retry_with_backoff`` / ``@with_retry``. Exponential backoff
  + jitter; ``CancelledError`` is never retried.

Use the decorator form when params are static (or pulled from a
module-level config); use the function form when params are dynamic per
call. Cancellation paths are translated into a predictable shape:

- caller-side timeout → ``ray.cancel(future)`` then re-raise ``TimeoutError``
- caller-side ``asyncio.CancelledError`` → ``ray.cancel(future)`` then re-raise
- worker-side ``TaskCancelledError`` → re-raise as-is
- worker-side ``RayTaskError`` → re-raise as ``RuntimeError`` (cause preserved)
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import random
from collections.abc import Callable
from typing import Any

import ray
from ray.exceptions import RayTaskError, TaskCancelledError
from utils.logger import get_logger

logger = get_logger()


def _resolve_description(template: str, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Format ``template`` with the wrapped call's bound arguments.

    A description like ``"PDF parse ({file_path})"`` gets ``{file_path}``
    substituted with the value passed to ``fn`` for that parameter. If
    ``template`` contains no ``{`` it is returned unchanged — no inspect
    cost in the hot path for plain-string descriptions.

    ``KeyError`` from a missing placeholder is caught and the raw
    template is returned, so a typo in a placeholder name degrades to a
    log-line oddity rather than a runtime crash on the wrapped call.
    """
    if "{" not in template:
        return template
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs).arguments
        return template.format(**bound)
    except (KeyError, TypeError) as exc:
        logger.warning(f"description template missing placeholder for {exc}")
        return template


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def call_ray_actor_with_timeout(
    future: ray.ObjectRef,
    timeout: float,
    task_description: str = "Ray task",
) -> Any:
    """Await a Ray ``ObjectRef`` with a timeout, propagating cancellation.

    Raises:
        TimeoutError: If the task exceeds ``timeout``.
        asyncio.CancelledError: If the calling coroutine is cancelled.
        TaskCancelledError: If the Ray task was cancelled by the worker.
        RuntimeError: If the Ray task failed (original exception chained).
    """
    try:
        result = await asyncio.wait_for(asyncio.gather(future), timeout=timeout)
        return result[0]

    except TimeoutError:
        logger.warning(f"{task_description} timed out, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise

    except asyncio.CancelledError:
        logger.warning(f"{task_description} cancelled, cancelling Ray task")
        ray.cancel(future, recursive=True)
        raise

    except TaskCancelledError:
        logger.warning(f"{task_description} Ray task was cancelled")
        raise

    except RayTaskError as e:
        raise RuntimeError(f"{task_description} failed") from e


def with_timeout(
    *,
    seconds: float,
    description: str = "Ray task",
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: wrap an async function returning a ``ray.ObjectRef``.

    The wrapped function is called normally; its return value (an
    ``ObjectRef``) is then awaited via ``call_ray_actor_with_timeout``.

    ``description`` may embed any of the wrapped function's parameter
    names as ``str.format``-style placeholders; they are substituted
    with the per-call argument values for log lines.

    Example::

        @with_timeout(
            seconds=30.0,
            description="caption_image ({path})",
        )
        async def caption(self, path):
            return self.actor.caption.remote(path)
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            desc = _resolve_description(description, fn, args, kwargs)
            future = fn(*args, **kwargs)
            if asyncio.iscoroutine(future):
                future = await future
            return await call_ray_actor_with_timeout(future, seconds, desc)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


async def retry_with_backoff(
    attempt_fn: Callable[[int], Any],
    max_retries: int,
    base_delay: float,
    task_description: str = "task",
    jitter: bool = True,
) -> Any:
    """Run ``attempt_fn(attempt_index)`` with exponential backoff.

    Backoff is ``base_delay * 2**attempt`` seconds, plus uniform jitter
    in ``[0, base_delay)`` when ``jitter=True``. ``attempt_fn`` is an
    async callable; it owns acquire/release of any per-attempt resources
    so a flaky resource can be sidestepped on retry.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await attempt_fn(attempt)
        except (asyncio.CancelledError, TaskCancelledError):
            raise
        except Exception as e:
            last_exc = e
            if attempt >= max_retries:
                logger.error(f"{task_description} failed after {attempt + 1} attempts: {e}")
                raise
            delay = base_delay * (2**attempt)
            if jitter:
                delay += random.uniform(0, base_delay)
            logger.warning(
                f"{task_description} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. Retrying in {delay:.1f}s..."
            )
            await asyncio.sleep(delay)

    raise last_exc  # unreachable


def with_retry(
    *,
    max_retries: int,
    base_delay: float,
    description: str = "task",
    jitter: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: retry an async function with exponential backoff + jitter.

    Each invocation counts as one attempt. ``CancelledError`` is never
    retried.

    ``description`` may embed any of the wrapped function's parameter
    names as ``str.format``-style placeholders; they are substituted
    with the per-call argument values for log lines.

    Example::

        @with_retry(
            max_retries=3,
            base_delay=0.5,
            description="transcribe ({path})",
        )
        async def transcribe(self, path):
            return await self.actor.transcribe.remote(path)
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            desc = _resolve_description(description, fn, args, kwargs)

            async def attempt(_i: int) -> Any:
                return await fn(*args, **kwargs)

            return await retry_with_backoff(
                attempt,
                max_retries=max_retries,
                base_delay=base_delay,
                task_description=desc,
                jitter=jitter,
            )

        return wrapper

    return decorator
