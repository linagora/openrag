# Adapter shim -- canonical code moved to services.auth.oidc_client (Phase 6F).
from services.auth.oidc_client import LogoutTokenClaims, OIDCClient, TokenBundle

__all__ = ["OIDCClient", "TokenBundle", "LogoutTokenClaims"]
