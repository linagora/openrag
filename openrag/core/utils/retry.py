"""Retry with exponential backoff, and finding a failure's original type.

Without Ray: ``services.workers.ray_utils`` builds its Ray-aware versions on
these, and code that runs outside Ray uses them directly.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()


def caused_by(exc: BaseException | None, types: type[BaseException] | tuple[type[BaseException], ...]) -> bool:
    """True when *exc*, or anything it was raised from, is one of *types*.

    Ray does not hand a worker's exception back unchanged: a child failure
    arrives as ``RayTaskError(Original)``, and :func:`call_ray_actor_with_timeout`
    then re-raises that as ``RuntimeError`` with the Ray error as ``__cause__``.
    A plain ``isinstance`` therefore misses the original type across an actor
    boundary — which is exactly where a caller needs to recognise it.

    Walks ``__cause__`` with a seen-set, because an explicitly chained cycle
    would otherwise spin forever.
    """
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, types):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


async def retry_with_backoff(
    attempt_fn: Callable[[int], Any],
    max_retries: int,
    base_delay: float,
    task_description: str = "task",
    jitter: bool = True,
    no_retry: tuple[type[BaseException], ...] = (),
    *,
    never_retry: tuple[type[BaseException], ...] = (asyncio.CancelledError,),
) -> Any:
    """Run ``attempt_fn(attempt_index)`` with exponential backoff.

    Backoff is ``base_delay * 2**attempt`` seconds, plus uniform jitter
    in ``[0, base_delay)`` when ``jitter=True``. ``attempt_fn`` is an
    async callable; it owns acquire/release of any per-attempt resources
    so a flaky resource can be sidestepped on retry.

    ``no_retry`` names failures a second attempt cannot change — a deterministic
    ceiling, say — so they are raised immediately instead of costing
    ``max_retries`` more runs and the backoff between them. Matched with
    :func:`caused_by`, not ``isinstance``: across an actor boundary the original
    type survives only on ``__cause__``. Empty by default, so every existing
    caller keeps retrying everything.

    ``never_retry`` are cancellations, re-raised as they are and never logged
    as a failure.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await attempt_fn(attempt)
        except never_retry:
            raise
        except Exception as e:
            last_exc = e
            if no_retry and caused_by(e, no_retry):
                logger.error(f"{task_description} failed with a non-retryable error: {e}")
                raise
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


__all__ = ["caused_by", "retry_with_backoff"]
