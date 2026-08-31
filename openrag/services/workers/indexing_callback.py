"""Best-effort status callbacks for async indexing outcomes.

Called once a file reaches a terminal state so clients don't have to poll the
task-status endpoint. Not a webhook: the cozy-stack target is a permission-checked
route (``POST /ai/index/status``) that 401s without a bearer token, hence
``callback_token``.

Never raises, no retries, one notification per file, 5 s timeout, strict no-op
without a ``callback_url`` — a callback must never affect the indexing outcome.

``file_rev`` is the file's CouchDB revision on the cozy side, compared there
against its own copy — recomputing it here would defeat that check. The
whitelist is deliberate: the upload metadata also holds ``source``,
``content_sha256`` and ``file_size``, and the target is a caller-supplied URL.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from urllib.parse import ParseResult, urlparse

import httpx
from core.config import load_config
from core.utils.logging import get_logger
from core.utils.url_safety import is_safe_url, resolves_to_public_addresses

logger = get_logger()

_CALLBACK_TIMEOUT = 5.0
_TIMEOUT = httpx.Timeout(_CALLBACK_TIMEOUT)

_ECHOED_METADATA_FIELDS = ("file_rev", "datetime", "doctype")


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
    "timestamp": <ISO8601 UTC>, "metadata": {"file_rev", "datetime", "doctype"}}``.

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

    allow_private = _allow_private_callback_urls()
    # Re-checked here, not just in the router: a direct caller bypasses that.
    unsafe = not is_safe_url(callback_url, allow_private_hosts=allow_private)
    if not unsafe and not allow_private:
        try:
            async with asyncio.timeout(_CALLBACK_TIMEOUT):
                unsafe = not await resolves_to_public_addresses(parsed.scheme, parsed.hostname or "", parsed.port)
        except TimeoutError:
            unsafe = True
    if unsafe:
        logger.warning(
            "Indexing callback_url is not a safe server-side target; skipping callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
        )
        return

    if callback_token and parsed.scheme != "https" and not allow_private:
        # A bearer over plain HTTP is a credential on the wire. The dev opt-in
        # is the exception: a local cozy serves http and still checks the token.
        logger.warning(
            "Refusing to send callback_token over plain HTTP; skipping callback",
            callback_url=safe_url,
            partition=partition,
            file_id=file_id,
            status=status,
        )
        return

    metadata = metadata or {}
    # No client-side skip on a missing file_rev: cozy-stack orders callbacks on
    # it and now 400s an empty one rather than risk applying it out of order.
    # Always present in echoed_metadata (None if absent) — the round-trip is
    # the receiver's call, not ours to shortcut.
    echoed_metadata = {field: metadata.get(field) for field in _ECHOED_METADATA_FIELDS}

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
