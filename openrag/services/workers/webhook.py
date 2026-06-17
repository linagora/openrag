"""Best-effort webhook notifications for async indexing outcomes.

Called by ``IndexerWorker`` once a file reaches a terminal state so that
external systems (e.g. cozy-stack) don't have to poll the task-status
endpoint.

Design constraints (kept identical to the main-branch implementation):
- Never raises: a callback failure must never affect the indexing outcome.
- No retries, single notification per file.
- callback_url absent → strict no-op.
- Timeout: 5 s on each of connect / read / write / pool.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from core.utils.logging import get_logger

logger = get_logger()

_TIMEOUT = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)


async def send_indexing_callback(
    callback_url: str | None,
    partition: str,
    file_id: str,
    status: str,
) -> None:
    """POST ``{"partition": …, "file_id": …, "status": "indexed"|"failed"}`` to *callback_url*.

    No-op when *callback_url* is ``None``.  Any network or HTTP error is
    logged as a warning and swallowed so the caller's outcome is unaffected.
    """
    if not callback_url:
        return

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                callback_url,
                json={"partition": partition, "file_id": file_id, "status": status},
            )
            response.raise_for_status()
    except Exception as exc:
        parsed = urlparse(callback_url)
        safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        logger.warning(
            "Failed to send indexing callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
            error=str(exc),
        )
