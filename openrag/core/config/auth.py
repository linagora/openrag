"""Authentication configuration — token mode + OIDC settings."""

from __future__ import annotations

from pydantic import BaseModel


class OIDCConfig(BaseModel):
    """OIDC configuration for Keycloak / external IdP integration.

    All fields are optional — if OIDC is not enabled, this section is ignored.
    Populated from environment variables (OIDC_ENDPOINT, OIDC_CLIENT_ID, etc.).
    """

    enabled: bool = False
    issuer_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid email profile offline_access"
    token_encryption_key: str = ""
    claim_source: str = "id_token"
    claim_mapping: str = ""
    post_logout_redirect_uri: str = ""
