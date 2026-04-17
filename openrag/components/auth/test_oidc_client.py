"""Unit tests for oidc_client.py — uses respx to mock httpx calls."""

import json
import time

import httpx
import pytest
import pytest_asyncio
import respx
from authlib.jose import JsonWebKey, OctKey

from components.auth.oidc_client import LogoutTokenClaims, OIDCClient, TokenBundle

# ---------------------------------------------------------------------------
# Helpers — RSA test key + JWT factory
# ---------------------------------------------------------------------------

ISSUER = "https://idp.example.com/realms/openrag"
CLIENT_ID = "openrag-client"
CLIENT_SECRET = "test-secret"
REDIRECT_URI = "https://openrag.example.com/auth/callback"
SCOPES = "openid email profile offline_access"


def _make_rsa_key_pair():
    """Generate an RSA-2048 key pair using authlib's JsonWebKey."""
    private = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    private_jwk = private.as_dict(is_private=True)
    public_jwk = private.as_dict()
    return private, private_jwk, public_jwk


# Generate once per module
_RSA_PRIVATE, _RSA_PRIVATE_JWK, _RSA_PUBLIC_JWK = _make_rsa_key_pair()
_RSA_PUBLIC_JWK["use"] = "sig"
_RSA_PUBLIC_JWK["alg"] = "RS256"
_RSA_PUBLIC_JWK.setdefault("kid", "test-key-1")
_RSA_PRIVATE_JWK.setdefault("kid", "test-key-1")

JWKS_RESPONSE = {"keys": [_RSA_PUBLIC_JWK]}

DISCOVERY_DOC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
    "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
    "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
}


def _sign_jwt(payload: dict) -> str:
    """Sign payload with the test RSA private key, returning a compact JWT string."""
    from authlib.jose import JsonWebToken

    header = {"alg": "RS256", "kid": "test-key-1"}
    jwt = JsonWebToken()
    token = jwt.encode(header, payload, _RSA_PRIVATE)
    # authlib returns bytes
    if isinstance(token, bytes):
        return token.decode()
    return token


def _id_token_payload(nonce: str, *, extra: dict | None = None) -> dict:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-sub-001",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": nonce,
        "email": "user@example.com",
    }
    if extra:
        payload.update(extra)
    return payload


def _logout_token_payload(*, sub: str | None = "user-sub-001", sid: str | None = None, extra: dict | None = None) -> dict:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "iat": now,
        "jti": "logout-jti-001",
        "events": {
            "http://schemas.openid.net/event/backchannel-logout": {}
        },
    }
    if sub is not None:
        payload["sub"] = sub
    if sid is not None:
        payload["sid"] = sid
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Fixture — OIDCClient with mocked httpx transport
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_transport():
    """Return a respx mock transport; caller activates with `with respx.mock(transport=...)`."""
    return respx.MockTransport()


@pytest_asyncio.fixture
async def client():
    """OIDCClient backed by a real httpx.AsyncClient using respx mock transport."""
    transport = respx.MockTransport(assert_all_called=False)
    http = httpx.AsyncClient(transport=transport)
    oc = OIDCClient(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scopes=SCOPES,
        http_client=http,
    )
    # Pre-register common mock routes on the transport's router
    oc._mock_transport = transport
    yield oc
    await oc.aclose()


def _setup_discovery(transport: respx.MockTransport):
    transport.router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY_DOC)
    )


def _setup_jwks(transport: respx.MockTransport):
    transport.router.get(f"{ISSUER}/protocol/openid-connect/certs").mock(
        return_value=httpx.Response(200, json=JWKS_RESPONSE)
    )


# ---------------------------------------------------------------------------
# PKCE generation tests (pure, no HTTP)
# ---------------------------------------------------------------------------

class TestPKCE:
    def test_verifier_length(self):
        verifier, _ = OIDCClient.generate_pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_urlsafe_base64(self):
        import base64
        import hashlib

        verifier, challenge = OIDCClient.generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected

    def test_unique_pairs(self):
        pairs = {OIDCClient.generate_pkce_pair()[0] for _ in range(20)}
        assert len(pairs) == 20

    def test_state_and_nonce_unique(self):
        states = {OIDCClient.generate_state_and_nonce()[0] for _ in range(20)}
        assert len(states) == 20


# ---------------------------------------------------------------------------
# Authorization URL
# ---------------------------------------------------------------------------

class TestBuildAuthorizationUrl:
    @pytest.mark.asyncio
    async def test_required_params(self, client):
        _setup_discovery(client._mock_transport)
        url = await client.build_authorization_url(
            state="mystate", nonce="mynonce", code_challenge="mychallenge"
        )
        assert "response_type=code" in url
        assert "client_id=openrag-client" in url
        assert "state=mystate" in url
        assert "nonce=mynonce" in url
        assert "code_challenge=mychallenge" in url
        assert "code_challenge_method=S256" in url
        assert url.startswith(DISCOVERY_DOC["authorization_endpoint"])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscover:
    @pytest.mark.asyncio
    async def test_issuer_mismatch_raises(self, client):
        bad_doc = dict(DISCOVERY_DOC, issuer="https://evil.example.com")
        client._mock_transport.router.get(
            f"{ISSUER}/.well-known/openid-configuration"
        ).mock(return_value=httpx.Response(200, json=bad_doc))
        with pytest.raises(ValueError, match="Issuer mismatch"):
            await client.discover()

    @pytest.mark.asyncio
    async def test_caching(self, client):
        _setup_discovery(client._mock_transport)
        doc1 = await client.discover()
        doc2 = await client.discover()
        # Same object from cache
        assert doc1 is doc2


# ---------------------------------------------------------------------------
# Code exchange
# ---------------------------------------------------------------------------

class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_success(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        nonce = "test-nonce-abc"
        id_token = _sign_jwt(_id_token_payload(nonce))
        token_response = {
            "id_token": id_token,
            "access_token": "at-123",
            "refresh_token": "rt-456",
            "expires_in": 300,
            "token_type": "Bearer",
        }
        client._mock_transport.router.post(
            f"{ISSUER}/protocol/openid-connect/token"
        ).mock(return_value=httpx.Response(200, json=token_response))

        bundle = await client.exchange_code(
            code="auth-code", code_verifier="verifier", expected_nonce=nonce
        )
        assert isinstance(bundle, TokenBundle)
        assert bundle.access_token == "at-123"
        assert bundle.refresh_token == "rt-456"
        assert bundle.claims["sub"] == "user-sub-001"
        assert bundle.claims["nonce"] == nonce

    @pytest.mark.asyncio
    async def test_nonce_mismatch_raises(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        id_token = _sign_jwt(_id_token_payload("correct-nonce"))
        token_response = {
            "id_token": id_token,
            "access_token": "at",
            "expires_in": 300,
            "token_type": "Bearer",
        }
        client._mock_transport.router.post(
            f"{ISSUER}/protocol/openid-connect/token"
        ).mock(return_value=httpx.Response(200, json=token_response))

        with pytest.raises(ValueError, match="nonce"):
            await client.exchange_code(
                code="code", code_verifier="v", expected_nonce="wrong-nonce"
            )


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_keeps_old_refresh_token_when_omitted(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        # IdP returns no refresh_token in the response
        token_response = {
            "access_token": "new-at",
            "expires_in": 300,
            "token_type": "Bearer",
            # no refresh_token
        }
        client._mock_transport.router.post(
            f"{ISSUER}/protocol/openid-connect/token"
        ).mock(return_value=httpx.Response(200, json=token_response))

        bundle = await client.refresh_access_token("old-rt")
        assert bundle.refresh_token == "old-rt"
        assert bundle.access_token == "new-at"

    @pytest.mark.asyncio
    async def test_uses_new_refresh_token_when_provided(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        token_response = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 300,
            "token_type": "Bearer",
        }
        client._mock_transport.router.post(
            f"{ISSUER}/protocol/openid-connect/token"
        ).mock(return_value=httpx.Response(200, json=token_response))

        bundle = await client.refresh_access_token("old-rt")
        assert bundle.refresh_token == "new-rt"


# ---------------------------------------------------------------------------
# Userinfo
# ---------------------------------------------------------------------------

class TestFetchUserinfo:
    @pytest.mark.asyncio
    async def test_returns_userinfo(self, client):
        _setup_discovery(client._mock_transport)

        userinfo = {"sub": "user-sub-001", "email": "user@example.com"}
        client._mock_transport.router.get(
            f"{ISSUER}/protocol/openid-connect/userinfo"
        ).mock(return_value=httpx.Response(200, json=userinfo))

        result = await client.fetch_userinfo("at-123")
        assert result["email"] == "user@example.com"


# ---------------------------------------------------------------------------
# Logout token verification
# ---------------------------------------------------------------------------

class TestVerifyLogoutToken:
    @pytest.mark.asyncio
    async def test_valid_logout_token_with_sub(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        token = _sign_jwt(_logout_token_payload(sub="user-sub-001"))
        claims = await client.verify_logout_token(token)
        assert isinstance(claims, LogoutTokenClaims)
        assert claims.sub == "user-sub-001"

    @pytest.mark.asyncio
    async def test_valid_logout_token_with_sid(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        token = _sign_jwt(_logout_token_payload(sub=None, sid="session-abc"))
        claims = await client.verify_logout_token(token)
        assert claims.sid == "session-abc"
        assert claims.sub is None

    @pytest.mark.asyncio
    async def test_missing_events_claim_raises(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        payload = _logout_token_payload()
        del payload["events"]
        token = _sign_jwt(payload)
        with pytest.raises(ValueError, match="back-channel-logout"):
            await client.verify_logout_token(token)

    @pytest.mark.asyncio
    async def test_wrong_events_key_raises(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        payload = _logout_token_payload()
        payload["events"] = {"http://schemas.openid.net/event/OTHER": {}}
        token = _sign_jwt(payload)
        with pytest.raises(ValueError, match="back-channel-logout"):
            await client.verify_logout_token(token)

    @pytest.mark.asyncio
    async def test_nonce_present_raises(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        payload = _logout_token_payload()
        payload["nonce"] = "forbidden"
        token = _sign_jwt(payload)
        with pytest.raises(ValueError, match="nonce"):
            await client.verify_logout_token(token)

    @pytest.mark.asyncio
    async def test_missing_sub_and_sid_raises(self, client):
        _setup_discovery(client._mock_transport)
        _setup_jwks(client._mock_transport)

        token = _sign_jwt(_logout_token_payload(sub=None, sid=None))
        with pytest.raises(ValueError, match="sub or sid"):
            await client.verify_logout_token(token)
