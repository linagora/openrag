"""Lazy, process-local singleton for the OIDCClient.

Kept in a dedicated module to avoid circular imports between the router
(``openrag/routers/auth.py``) and the application entry point (``openrag/api.py``).

The OIDC config env vars are resolved here via ``os.getenv`` — the same values
that ``openrag/api.py`` validates at startup. In ``AUTH_MODE=oidc`` mode, these
are guaranteed to be non-empty (api.py refuses to start otherwise), so this
module simply trusts them.
"""

from __future__ import annotations

import os
from threading import Lock

from components.auth.oidc_client import OIDCClient

_client: OIDCClient | None = None
_lock = Lock()


def get_oidc_client() -> OIDCClient:
    """Return the shared OIDCClient instance, creating it on first call.

    The instance caches the discovery doc and JWKS, so a single shared client
    per worker process is both correct and more efficient than one-per-request.

    Env vars read (all required in AUTH_MODE=oidc):
      - OIDC_ENDPOINT
      - OIDC_CLIENT_ID
      - OIDC_CLIENT_SECRET
      - OIDC_REDIRECT_URI
      - OIDC_SCOPES (default ``openid email profile offline_access``)
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        issuer = os.environ["OIDC_ENDPOINT"]
        client_id = os.environ["OIDC_CLIENT_ID"]
        client_secret = os.environ["OIDC_CLIENT_SECRET"]
        redirect_uri = os.environ["OIDC_REDIRECT_URI"]
        scopes = os.getenv("OIDC_SCOPES", "openid email profile offline_access")
        _client = OIDCClient(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
    return _client


def reset_oidc_client() -> None:
    """Test hook — drops the cached client so the next call rebuilds from env.

    Best-effort closes the underlying httpx.AsyncClient to avoid "Unclosed
    client session" warnings and leaking connections when tests repeatedly
    reset the singleton. If no event loop is running we skip the close call
    — the GC will eventually reclaim the socket.
    """
    global _client
    with _lock:
        old = _client
        _client = None
    if old is None:
        return
    try:
        import asyncio

        loop = asyncio.get_event_loop_policy().get_event_loop()
        if loop.is_running():
            # Schedule close on the running loop without awaiting — caller
            # doesn't need to be async.
            loop.create_task(old.aclose())
        else:
            loop.run_until_complete(old.aclose())
    except Exception:
        # Closing is best-effort; never let a reset blow up the caller.
        pass
