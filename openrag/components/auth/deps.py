"""Compatibility shim - implementation lives in services.auth.deps."""

from __future__ import annotations

from services.auth.deps import get_oidc_client as _service_get_oidc_client
from services.auth.deps import reset_oidc_client as _service_reset_oidc_client
from services.auth.oidc_client import OIDCClient

_client: OIDCClient | None = None


def get_oidc_client() -> OIDCClient:
    if _client is not None:
        return _client
    return _service_get_oidc_client()


def reset_oidc_client() -> None:
    global _client
    _client = None
    _service_reset_oidc_client()

__all__ = ["get_oidc_client", "reset_oidc_client"]
