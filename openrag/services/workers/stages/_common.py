from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

_CREDENTIAL_KEYS = frozenset({"credentials", "credential", "api_key", "token", "secret", "password"})


def scrub_credentials(row: MutableMapping[str, Any]) -> None:
    for key in _CREDENTIAL_KEYS:
        row.pop(key, None)


def stage_timeout(base_timeout: float | None, item_count: int, *, per_item_timeout: float = 0.0) -> float | None:
    if base_timeout is None:
        return None
    return base_timeout + max(0, item_count) * per_item_timeout


async def run_with_optional_timeout[T](
    operation: Callable[[], Awaitable[T]],
    timeout: float | None,
) -> T:
    if timeout is None:
        return await operation()
    return await asyncio.wait_for(operation(), timeout=timeout)
