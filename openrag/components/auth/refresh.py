"""Lazy refresh helper for OIDC access tokens.

Extracted from ``AuthMiddleware`` (Phase 5) to keep ``api.py`` small and
independently testable. Called per-request when a valid cookie session is
found; a no-op when the access token is still fresh.

Timezone policy
---------------
Phase 2 stores all OIDC session timestamps as **naive local time** via
``datetime.now()`` (see ``test_oidc_sessions.py`` and
``PartitionFileManager.get_oidc_session_by_token``). We match that style
everywhere in this module to avoid tz-mismatch bugs when comparing
``access_token_expires_at`` against "now".

Refresh-token stampede guard (M1)
---------------------------------
IdPs with refresh_token rotation enabled invalidate the old refresh_token the
first time it is redeemed. Under concurrency, multiple requests can each notice
"my access_token is about to expire" at the same time and race each other to
the token endpoint. The second attempt fails with ``invalid_grant`` and
(without a guard) its session would be revoked mid-flight.

We mitigate that with two cooperating mechanisms:

1. A **short-circuit** here: if ``last_refresh_at`` was bumped less than 5
   seconds ago, we assume a sibling request already rotated the tokens,
   re-read the row, and reuse those freshly rotated tokens instead of calling
   the IdP.
2. A **row-level write lock** in :meth:`PartitionFileManager.update_oidc_session_tokens`
   (``SELECT ... FOR UPDATE``) so that only one writer commits at a time on
   Postgres.
3. An **error-recovery branch** here: if the IdP does reject our refresh_token
   (typically because a sibling raced us and won), we re-read the row once
   more and, if the tokens were advanced meanwhile, return the fresh session
   rather than giving up.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from components.auth.deps import get_oidc_client
from components.auth.session_tokens import decrypt_token, encrypt_token
from utils.logger import get_logger

_REFRESH_BUFFER = timedelta(seconds=60)
_STAMPEDE_WINDOW = timedelta(seconds=5)

logger = get_logger()


def _to_dt(val: Any) -> datetime:
    """Coerce a datetime-or-ISO-string into a ``datetime``.

    Ray occasionally ships values across actors in serialised form; accept
    either shape so callers never have to care about the transport.
    """
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        return datetime.fromisoformat(val)
    raise TypeError(f"Expected datetime or ISO string, got {type(val).__name__}")


async def refresh_session_if_needed(
    *,
    session: dict[str, Any],
    enc_key: str,
    vectordb: Any,
) -> dict[str, Any] | None:
    """Refresh the IdP access_token if it is within ``_REFRESH_BUFFER`` of expiry.

    Behaviour:
      - If the access_token is still valid with the 60s buffer → return ``session`` unchanged.
      - Stampede guard: if another request has just refreshed this session
        (``last_refresh_at`` within 5s), re-read the row and reuse the fresh
        tokens without calling the IdP.
      - If near/past expiry AND a ``refresh_token_encrypted`` blob is stored →
        call the IdP, persist rotated tokens, return an updated session dict.
      - If near/past expiry AND no refresh_token is stored → return ``session`` as-is
        when still formally valid, or ``None`` when already expired (caller should
        treat as a revoked session).
      - If the refresh call raises (typically because a sibling already rotated
        the tokens and the IdP now rejects ours) → re-read the row; if a sibling
        succeeded, return their fresh session; otherwise ``None``.

    The session dict returned mirrors the DB row shape produced by
    ``PartitionFileManager._oidc_session_to_dict``.
    """
    now = datetime.now()
    access_exp = _to_dt(session["access_token_expires_at"])

    if access_exp > now + _REFRESH_BUFFER:
        return session

    # --- Stampede short-circuit -------------------------------------------
    # If a sibling request just refreshed this same session, re-read the row
    # and reuse the freshly rotated tokens. This avoids racing the IdP with a
    # refresh_token that the sibling's success has already invalidated.
    last_refresh_at = session.get("last_refresh_at")
    if last_refresh_at is not None:
        try:
            last_refresh_at_dt = _to_dt(last_refresh_at)
        except TypeError:
            last_refresh_at_dt = None
        if (
            last_refresh_at_dt is not None
            and (now - last_refresh_at_dt) < _STAMPEDE_WINDOW
        ):
            try:
                fresh = await vectordb.get_oidc_session_by_id.remote(session["id"])
            except Exception as e:
                logger.bind(session_id=session.get("id"), error=str(e)).warning(
                    "Stampede-guard re-read failed; falling through to refresh"
                )
                fresh = None
            if fresh is not None:
                fresh_exp = _to_dt(fresh["access_token_expires_at"])
                if fresh_exp > now + _REFRESH_BUFFER:
                    return fresh

    refresh_enc = session.get("refresh_token_encrypted")
    if not refresh_enc:
        # No refresh_token available.
        # - If still formally valid (within the 60s buffer window but not yet past exp),
        #   keep using it.
        # - If already expired, caller should treat the session as dead.
        return session if access_exp > now else None

    try:
        refresh_token = decrypt_token(refresh_enc, enc_key)
        client = get_oidc_client()
        bundle = await client.refresh_access_token(refresh_token)
    except Exception as e:
        # Maybe a sibling refreshed between our staleness check and the IdP call
        # and the IdP has already invalidated our refresh_token. Re-read the
        # row once before giving up: if the tokens were rotated meanwhile,
        # treat this as a successful refresh (the sibling's).
        logger.bind(session_id=session.get("id"), error=str(e)).warning(
            "OIDC refresh_token exchange failed — re-reading session for stampede recovery"
        )
        try:
            fresh = await vectordb.get_oidc_session_by_id.remote(session["id"])
        except Exception as re:
            logger.bind(session_id=session.get("id"), error=str(re)).error(
                "Post-failure re-read of OIDC session failed — invalidating"
            )
            return None
        if fresh is not None:
            fresh_exp = _to_dt(fresh["access_token_expires_at"])
            if fresh_exp > now + _REFRESH_BUFFER:
                return fresh
        return None

    new_access_exp = now + timedelta(seconds=max(int(bundle.expires_in or 0), 60))
    new_access_enc = encrypt_token(bundle.access_token, enc_key)
    new_refresh_enc = (
        encrypt_token(bundle.refresh_token, enc_key)
        if bundle.refresh_token
        else refresh_enc
    )

    try:
        await vectordb.update_oidc_session_tokens.remote(
            session_id=session["id"],
            access_token_encrypted=new_access_enc,
            refresh_token_encrypted=new_refresh_enc,
            access_token_expires_at=new_access_exp,
        )
    except Exception as e:
        logger.bind(session_id=session.get("id"), error=str(e)).error(
            "Failed to persist refreshed OIDC tokens — invalidating session"
        )
        return None

    return {
        **session,
        "access_token_encrypted": new_access_enc,
        "access_token_expires_at": new_access_exp,
        "refresh_token_encrypted": new_refresh_enc,
        "last_refresh_at": now,
    }
