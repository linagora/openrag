# Adapter shim -- canonical code moved to services.auth.deps (Phase 6F).
from services.auth.deps import get_oidc_client, reset_oidc_client

__all__ = ["get_oidc_client", "reset_oidc_client"]
