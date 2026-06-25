from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("authlib")

from authlib.jose import JsonWebKey, JsonWebToken  # noqa: E402
from services.auth import oidc_client as oidc_module  # noqa: E402
from services.auth.oidc_client import OIDCClient  # noqa: E402

ISSUER = "https://idp.example.com/realms/openrag"
CLIENT_ID = "openrag-client"

_RSA_PRIVATE = JsonWebKey.generate_key("RSA", 2048, is_private=True)
_RSA_PUBLIC_JWK = _RSA_PRIVATE.as_dict()
_RSA_PUBLIC_JWK["use"] = "sig"
_RSA_PUBLIC_JWK["alg"] = "RS256"
_RSA_PUBLIC_JWK["kid"] = "test-key-1"
JWKS_RESPONSE = {"keys": [_RSA_PUBLIC_JWK]}


def _sign_jwt(payload: dict[str, Any]) -> str:
    token = JsonWebToken(["RS256"]).encode({"alg": "RS256", "kid": "test-key-1"}, payload, _RSA_PRIVATE)
    return token.decode() if isinstance(token, bytes) else token


def _logout_token(*, iat: int, exp: int | None = None) -> str:
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": iat,
        "jti": "logout-jti-1",
        "events": {"http://schemas.openid.net/event/backchannel-logout": {}},
        "sid": "sid-1",
        "sub": "sub-1",
    }
    if exp is not None:
        payload["exp"] = exp
    return _sign_jwt(payload)


def _client() -> OIDCClient:
    client = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="secret",
        redirect_uri="https://openrag.example.com/auth/callback",
        scopes="openid",
    )
    client._metadata = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/certs"}
    client._metadata_fetched_at = time.time()
    client._jwks = JsonWebKey.import_key_set(JWKS_RESPONSE)
    client._jwks_fetched_at = time.time()
    return client


@pytest.mark.asyncio
async def test_verify_logout_token_accepts_current_token_without_exp(monkeypatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(oidc_module.time, "time", lambda: now)

    claims = await _client().verify_logout_token(_logout_token(iat=now))

    assert claims.jti == "logout-jti-1"
    assert claims.exp is None


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_stale_token_without_exp(monkeypatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(oidc_module.time, "time", lambda: now)

    token = _logout_token(iat=now - 10_000)

    with pytest.raises(ValueError, match="too old"):
        await _client().verify_logout_token(token)


@pytest.mark.asyncio
async def test_verify_logout_token_rejects_future_iat_without_exp(monkeypatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(oidc_module.time, "time", lambda: now)

    token = _logout_token(iat=now + 120)

    with pytest.raises(ValueError, match="not yet valid"):
        await _client().verify_logout_token(token)
