"""Auth service layer - OIDC client, session tokens, state cookie, and deps."""

from core.auth.state_cookie import StateCookiePayload, StateCookieSerializer

from .deps import get_oidc_client, reset_oidc_client
from .oidc_client import LogoutTokenClaims, OIDCClient, TokenBundle
from .session_tokens import decrypt_token, encrypt_token, hash_session_token, issue_session_token

__all__ = [
    "OIDCClient",
    "TokenBundle",
    "LogoutTokenClaims",
    "issue_session_token",
    "encrypt_token",
    "decrypt_token",
    "hash_session_token",
    "StateCookieSerializer",
    "StateCookiePayload",
    "get_oidc_client",
    "reset_oidc_client",
]
