"""Lightweight OIDC Relying Party client for OpenRAG.

Wraps Authlib's JWT/JWK primitives with:
- Discovery endpoint caching (1 h TTL)
- JWKS caching with automatic refresh on kid-miss
- PKCE pair generation (S256)
- Authorization URL builder
- Code exchange with ID token verification
- Token refresh (lazy, called by middleware when access_token near expiry)
- Userinfo fetch
- Back-channel logout token verification

One instance per (issuer, client_id, client_secret) tuple.
The instance is not thread-safe for writes but safe for concurrent reads once
the metadata and JWKS caches are populated.
"""

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError


@dataclass
class TokenBundle:
    """Holds the token set returned by the IdP together with verified ID token claims."""

    id_token: str
    access_token: str
    refresh_token: str | None
    expires_in: int         # seconds
    token_type: str         # usually "Bearer"
    claims: dict[str, Any]  # verified claims from id_token


@dataclass
class LogoutTokenClaims:
    """Verified claims from a back-channel logout token."""

    iss: str
    aud: str | list[str]
    sub: str | None
    sid: str | None
    iat: int
    jti: str | None


class OIDCClient:
    """Lightweight OIDC Relying Party client.

    One instance per (issuer, client_id, client_secret) tuple.
    """

    _DISCOVERY_TTL = 3600  # 1 hour
    _JWKS_TTL = 3600       # 1 hour

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        http_client: httpx.AsyncClient | None = None,
    ):
        # Keep the issuer string verbatim (including any trailing "/") — the OIDC
        # spec mandates strict byte-for-byte equality between ``self.issuer``, the
        # issuer advertised by the discovery document, and the ``iss`` claim in
        # tokens. Operators must configure ``OIDC_ENDPOINT`` to match EXACTLY
        # what the IdP returns.
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._metadata: dict | None = None
        self._metadata_fetched_at: float = 0.0
        self._jwks: JsonWebKey | None = None
        self._jwks_fetched_at: float = 0.0

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover(self) -> dict:
        """Fetch and cache the OIDC discovery document.

        Returns the cached document if it is less than _DISCOVERY_TTL seconds old.
        Raises ValueError if the returned issuer does not match the configured one.
        """
        if self._metadata and (time.time() - self._metadata_fetched_at) < self._DISCOVERY_TTL:
            return self._metadata
        url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        resp = await self._http.get(url)
        resp.raise_for_status()
        self._metadata = resp.json()
        self._metadata_fetched_at = time.time()
        if self._metadata.get("issuer") != self.issuer:
            raise ValueError(
                f"Issuer mismatch: configured {self.issuer!r}, got {self._metadata.get('issuer')!r}"
            )
        return self._metadata

    # ------------------------------------------------------------------
    # JWKS
    # ------------------------------------------------------------------

    async def _load_jwks(self, force: bool = False) -> JsonWebKey:
        meta = await self.discover()
        if not force and self._jwks and (time.time() - self._jwks_fetched_at) < self._JWKS_TTL:
            return self._jwks
        resp = await self._http.get(meta["jwks_uri"])
        resp.raise_for_status()
        self._jwks = JsonWebKey.import_key_set(resp.json())
        self._jwks_fetched_at = time.time()
        return self._jwks

    # ------------------------------------------------------------------
    # PKCE helpers
    # ------------------------------------------------------------------

    @staticmethod
    def generate_pkce_pair() -> tuple[str, str]:
        """Generate a PKCE (code_verifier, code_challenge) pair using S256.

        Returns:
            (verifier, challenge) — verifier is 128 url-safe chars,
            challenge is the base64url-encoded SHA-256 of the verifier.
        """
        verifier = secrets.token_urlsafe(96)[:128]
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    @staticmethod
    def generate_state_and_nonce() -> tuple[str, str]:
        """Generate cryptographically random state and nonce values."""
        return secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    # ------------------------------------------------------------------
    # Authorization URL
    # ------------------------------------------------------------------

    async def build_authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        """Build the full authorization URL to redirect the browser to."""
        meta = await self.discover()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Code exchange
    # ------------------------------------------------------------------

    async def exchange_code(
        self, *, code: str, code_verifier: str, expected_nonce: str
    ) -> TokenBundle:
        """Exchange an authorization code for tokens.

        Verifies the returned id_token (signature, iss, aud, exp, nonce).

        Args:
            code: The authorization code from the IdP callback.
            code_verifier: The PKCE verifier corresponding to the challenge sent earlier.
            expected_nonce: The nonce value that was sent in the authorization request.

        Returns:
            A TokenBundle with verified claims.
        """
        meta = await self.discover()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": code_verifier,
        }
        resp = await self._http.post(
            meta["token_endpoint"], data=data, headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        payload = resp.json()
        id_token = payload["id_token"]
        claims = await self._verify_id_token(id_token, expected_nonce=expected_nonce)
        return TokenBundle(
            id_token=id_token,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in", 0)),
            token_type=payload.get("token_type", "Bearer"),
            claims=claims,
        )

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_access_token(self, refresh_token: str) -> TokenBundle:
        """Use the refresh_token to obtain a new access_token.

        If the IdP returns a new id_token, it is re-verified (nonce check skipped
        per RFC 8252 §8.2 — nonce is only required during the initial code exchange).
        If the IdP omits the refresh_token in the response, the caller's existing
        refresh_token is preserved.

        Returns:
            A new TokenBundle.
        """
        meta = await self.discover()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = await self._http.post(
            meta["token_endpoint"], data=data, headers={"Accept": "application/json"}
        )
        resp.raise_for_status()
        payload = resp.json()
        new_id_token = payload.get("id_token")
        claims: dict[str, Any] = {}
        if new_id_token:
            claims = await self._verify_id_token(new_id_token, expected_nonce=None)
        return TokenBundle(
            id_token=new_id_token or "",
            access_token=payload["access_token"],
            # Some IdPs omit the refresh_token on rotation — keep the old one.
            refresh_token=payload.get("refresh_token", refresh_token),
            expires_in=int(payload.get("expires_in", 0)),
            token_type=payload.get("token_type", "Bearer"),
            claims=claims,
        )

    # ------------------------------------------------------------------
    # Userinfo
    # ------------------------------------------------------------------

    async def fetch_userinfo(self, access_token: str) -> dict:
        """Fetch the userinfo endpoint with the given access token."""
        meta = await self.discover()
        resp = await self._http.get(
            meta["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # ID token verification
    # ------------------------------------------------------------------

    async def _verify_id_token(
        self, token: str, *, expected_nonce: str | None
    ) -> dict[str, Any]:
        """Verify an ID token's signature and standard claims.

        Retries with a fresh JWKS fetch on kid-miss (covers IdP key rotation).
        Raises JoseError / ValueError on any validation failure.
        """
        jwks = await self._load_jwks()
        jwt = JsonWebToken(["RS256", "ES256", "EdDSA", "RS384", "RS512"])
        try:
            claims = jwt.decode(token, jwks)
        except JoseError:
            # Force JWKS refresh in case of kid rotation; retry once.
            jwks = await self._load_jwks(force=True)
            claims = jwt.decode(token, jwks)

        # Manual validation — avoids authlib version differences around claims.params
        decoded: dict[str, Any] = dict(claims)
        now = int(time.time())

        if decoded.get("iss") != self.issuer:
            raise ValueError(f"ID token iss mismatch: expected {self.issuer!r}, got {decoded.get('iss')!r}")

        aud = decoded.get("aud")
        if isinstance(aud, list):
            if self.client_id not in aud:
                raise ValueError(f"ID token aud {aud!r} does not contain client_id {self.client_id!r}")
        elif aud != self.client_id:
            raise ValueError(f"ID token aud {aud!r} != client_id {self.client_id!r}")

        if "exp" not in decoded:
            raise ValueError("ID token missing exp claim")
        if int(decoded["exp"]) < now:
            raise ValueError("ID token has expired")

        if "iat" not in decoded:
            raise ValueError("ID token missing iat claim")

        if expected_nonce is not None:
            if decoded.get("nonce") != expected_nonce:
                raise ValueError("OIDC nonce mismatch")

        return decoded

    # ------------------------------------------------------------------
    # Back-channel logout token verification
    # ------------------------------------------------------------------

    async def verify_logout_token(self, token: str) -> LogoutTokenClaims:
        """Verify an OIDC back-channel logout token.

        Validates:
        - Signature (with JWKS kid-miss retry)
        - Standard claims (iss, aud, iat)
        - events claim contains the back-channel-logout URI key
        - nonce must NOT be present (spec requirement)
        - At least one of sub or sid must be present

        Returns:
            LogoutTokenClaims with the verified values.
        Raises:
            ValueError: on any spec violation.
        """
        jwks = await self._load_jwks()
        jwt = JsonWebToken(["RS256", "ES256", "EdDSA", "RS384", "RS512"])
        try:
            claims = jwt.decode(token, jwks)
        except JoseError:
            jwks = await self._load_jwks(force=True)
            claims = jwt.decode(token, jwks)

        decoded: dict[str, Any] = dict(claims)
        now = int(time.time())

        if decoded.get("iss") != self.issuer:
            raise ValueError(f"logout_token iss mismatch: expected {self.issuer!r}, got {decoded.get('iss')!r}")

        aud = decoded.get("aud")
        if isinstance(aud, list):
            if self.client_id not in aud:
                raise ValueError(f"logout_token aud {aud!r} does not contain client_id {self.client_id!r}")
        elif aud != self.client_id:
            raise ValueError(f"logout_token aud {aud!r} != client_id {self.client_id!r}")

        if "iat" not in decoded:
            raise ValueError("logout_token missing iat claim")
        if int(decoded.get("exp", now + 1)) < now:
            raise ValueError("logout_token has expired")

        events = decoded.get("events") or {}
        if "http://schemas.openid.net/event/backchannel-logout" not in events:
            raise ValueError("logout_token missing required back-channel-logout event claim")

        if decoded.get("nonce"):
            raise ValueError("logout_token must not contain nonce")

        if not decoded.get("sub") and not decoded.get("sid"):
            raise ValueError("logout_token must contain sub or sid")

        return LogoutTokenClaims(
            iss=decoded["iss"],
            aud=decoded["aud"],
            sub=decoded.get("sub"),
            sid=decoded.get("sid"),
            iat=int(decoded["iat"]),
            jti=decoded.get("jti"),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()
