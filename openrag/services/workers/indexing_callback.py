"""Best-effort status callbacks for async indexing outcomes.

Not a webhook: the cozy-stack target is a permission-checked route that 401s
without a bearer token, hence ``callback_token``.

Never raises, no retries, one notification per file, 5 s timeout, strict no-op
without a ``callback_url``.

``metadata`` is echoed back verbatim except for ``UPLOAD_METADATA_SERVER_KEYS``
(the server-computed fields, on-disk path chief among them, that must not
reach a caller-supplied URL). Any other caller-supplied key, named however the
caller likes, travels through unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx
from core.config import load_config
from core.utils.consts import UPLOAD_METADATA_SERVER_KEYS
from core.utils.logging import get_logger
from core.utils.url_safety import is_safe_url

logger = get_logger()

_CALLBACK_TIMEOUT = 5.0
_TIMEOUT = httpx.Timeout(_CALLBACK_TIMEOUT)


def _allow_private_callback_urls() -> bool:
    try:
        return bool(load_config().indexing_callback.allow_private_urls)
    except Exception:
        return False


def _describe_error(exc: Exception, safe_url: str) -> str:
    """Return a description of a failed callback that is safe to log.

    We don't log str(exc): httpx puts the full URL (credentials, path, query,
    fragment) and the redirect location in its error messages. We only keep
    the exception type, the HTTP status code and the host.
    """
    description = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        description += f" (HTTP {exc.response.status_code})"
    return f"{description} for {safe_url}"


async def send_indexing_callback(
    callback_url: str | None,
    partition: str,
    file_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
    callback_token: str | None = None,
) -> None:
    """POST the indexing outcome to *callback_url*.

    Body: ``{"partition", "file_id", "status": "success"|"error",
    "metadata": <upload metadata minus UPLOAD_METADATA_SERVER_KEYS>}``.

    *callback_token* is sent as ``Authorization: Bearer <token>``. No-op when
    *callback_url* is ``None``. Any network or HTTP error is logged and
    swallowed so the caller's outcome is unaffected.
    """
    if not callback_url:
        return

    try:
        parsed = urlparse(callback_url)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        # Scheme and host only: path/query/userinfo can carry secrets.
        safe_url = f"{parsed.scheme}://{netloc}"
    except Exception as exc:
        logger.warning(
            "Invalid indexing callback_url; skipping callback",
            partition=partition,
            file_id=file_id,
            status=status,
            # Only the type: the message can quote part of the password (parsed as the port).
            error=type(exc).__name__,
        )
        return

    # Re-checked here, not just in the router: a direct caller bypasses that.
    if not is_safe_url(callback_url, allow_private_hosts=_allow_private_callback_urls()):
        logger.warning(
            "Indexing callback_url is not a safe server-side target; skipping callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
        )
        return

    metadata = metadata or {}
    echoed_metadata = {key: value for key, value in metadata.items() if key not in UPLOAD_METADATA_SERVER_KEYS}

    payload = {
        "partition": partition,
        "file_id": file_id,
        "status": status,
        "metadata": echoed_metadata,
    }

    # Header only — a query-string token would land in the target's access logs.
    headers = {"Authorization": f"Bearer {callback_token}"} if callback_token else {}

    try:
        async with asyncio.timeout(_CALLBACK_TIMEOUT), httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(callback_url, json=payload, headers=headers)
            response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "Failed to send indexing callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
            error=_describe_error(exc, safe_url),
        )
