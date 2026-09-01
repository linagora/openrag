"""Best-effort status callbacks for async indexing outcomes.

Called once a file reaches a terminal state so clients don't have to poll the
task-status endpoint. Not a webhook: the cozy-stack target is a permission-checked
route (``POST /ai/index/status``) that 401s without a bearer token, hence
``callback_token``.

Never raises, no retries, one notification per file, 5 s timeout, strict no-op
without a ``callback_url`` — a callback must never affect the indexing outcome.

``metadata`` is echoed back verbatim except for ``UPLOAD_METADATA_SERVER_KEYS``
(``source``, ``content_sha256``, ``file_size``, ...): the caller learns nothing
new from getting its own fields back, but those server-computed ones — the
on-disk path chief among them — must not reach a caller-supplied URL. Any
revision-tracking field a caller sends (e.g. cozy-stack's ``doc_rev``) travels
through unchanged; this module has no opinion on its name.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import ParseResult, urlparse

import httpx
from core.config import load_config
from core.utils.conts import UPLOAD_METADATA_SERVER_KEYS
from core.utils.logging import get_logger
from core.utils.url_safety import is_safe_url

logger = get_logger()

_CALLBACK_TIMEOUT = 5.0
_TIMEOUT = httpx.Timeout(_CALLBACK_TIMEOUT)


def _allow_private_callback_urls() -> bool:
    """Whether the operator opted out of the address half of the SSRF guard."""
    try:
        return bool(load_config().indexing_callback.allow_private_urls)
    except Exception:
        return False


def _url_credentials(callback_url: str, parsed: ParseResult) -> tuple[str, ...]:
    """Credential substrings of *callback_url* to scrub from any logged message.

    Over-redacting is safe here: every result is a secret.
    """
    secrets: list[str] = []
    if "@" in parsed.netloc:
        secrets.append(parsed.netloc.rsplit("@", 1)[0])
    for part in (parsed.password, parsed.username):
        if part:
            secrets.append(part)
    try:
        userinfo = httpx.URL(callback_url).userinfo.decode("ascii", errors="ignore")
    except Exception:
        # httpx rejects some URLs urlparse accepts (e.g. a non-numeric port).
        userinfo = ""
    if userinfo:
        secrets.append(userinfo)
        secrets.extend(part for part in userinfo.split(":", 1) if part)
    # Longest first: redacting "user" before "user:pass" would leave the
    # password behind in "REDACTED:pass".
    return tuple(sorted(set(secrets), key=len, reverse=True))


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
    "timestamp": <ISO8601 UTC>, "metadata": <upload metadata minus
    UPLOAD_METADATA_SERVER_KEYS>}``.

    *callback_token* is sent as ``Authorization: Bearer <token>``; without one no
    header is added, which is the pre-existing behaviour for unauthenticated
    endpoints. No-op when *callback_url* is ``None``. Any network or HTTP error
    is logged and swallowed so the caller's outcome is unaffected.
    """
    if not callback_url:
        return

    try:
        parsed = urlparse(callback_url)
        # Rebuilt from hostname/port: ``netloc`` would carry userinfo into logs.
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        # Path dropped too: a webhook secret conventionally lives there
        # ("/hooks/T000/B000/XXXX"), the same class of leak as userinfo and the
        # query string. Scheme and host are enough to identify the target.
        safe_url = f"{parsed.scheme}://{netloc}"
        # httpx puts the full URL, userinfo included, in its exception messages.
        # Collected here so a malformed URL cannot crash the handler below.
        url_secrets = _url_credentials(callback_url, parsed)
    except Exception as exc:
        # Malformed callback_url (e.g. an unterminated IPv6 literal) must not
        # turn a successful indexing run into a reported failure.
        logger.warning(
            "Invalid indexing callback_url; skipping callback",
            partition=partition,
            file_id=file_id,
            status=status,
            error=str(exc),
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
    # Exclusion, not a fixed field list: any caller-supplied key (a revision
    # marker, an app-specific tag, whatever they sent at upload) travels
    # through as-is. We don't second-guess how the receiver validates it —
    # e.g. cozy-stack now 400s an empty revision rather than apply it out of
    # order, which is exactly the receiver's call to make, not ours to
    # pre-empt by dropping or defaulting the field ourselves.
    echoed_metadata = {key: value for key, value in metadata.items() if key not in UPLOAD_METADATA_SERVER_KEYS}

    payload = {
        "partition": partition,
        "file_id": file_id,
        "status": status,
        # "Z" and milliseconds: the spelling cozy-stack documents. Plain
        # isoformat() gives "+00:00" and microseconds — valid, but not that.
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "metadata": echoed_metadata,
    }

    # Header only — a query-string token lands in the target's access logs.
    headers = {"Authorization": f"Bearer {callback_token}"} if callback_token else {}

    try:
        # One deadline over the whole request: httpx's per-phase timeouts would
        # let connect + write + read each spend the full budget in turn.
        async with asyncio.timeout(_CALLBACK_TIMEOUT), httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(callback_url, json=payload, headers=headers)
            response.raise_for_status()
    except Exception as exc:
        error_message = str(exc)
        for secret in url_secrets:
            error_message = error_message.replace(secret, "REDACTED")
        if callback_token:
            error_message = error_message.replace(callback_token, "REDACTED")
        if len(parsed.path) > 1:
            # httpx echoes the full URL, path included. Guarded on length so a
            # bare "/" does not rewrite every slash in the message.
            error_message = error_message.replace(parsed.path, "/REDACTED")
            # httpx percent-encodes the path the same way it does the query
            # (spaces, non-ASCII, etc.), so the raw substring above misses a
            # secret-in-path (a webhook-style URL) containing those characters.
            try:
                # ``raw_path`` is "path?query" together; keep only the path
                # half so this does not also eat the query string, which the
                # block below redacts (and logs) on its own.
                encoded_path = httpx.URL(callback_url).raw_path.split(b"?", 1)[0].decode("ascii", errors="ignore")
                if encoded_path and encoded_path != parsed.path:
                    error_message = error_message.replace(encoded_path, "/REDACTED")
            except Exception:
                pass
        if parsed.query:
            # httpx percent-encodes the query, so the message may embed either
            # spelling. Raw first — that one cannot fail.
            error_message = error_message.replace(parsed.query, "REDACTED")
            try:
                encoded_query = httpx.URL(callback_url).query.decode("ascii", errors="ignore")
                if encoded_query:
                    error_message = error_message.replace(encoded_query, "REDACTED")
            except Exception:
                pass
        logger.warning(
            "Failed to send indexing callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
            error=error_message,
        )
