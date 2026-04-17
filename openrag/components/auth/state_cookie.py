"""Signed state cookie for OIDC Authorization Code + PKCE flow.

The cookie transports state/nonce/code_verifier between /auth/login and /auth/callback.
It is signed (not encrypted) using itsdangerous.URLSafeTimedSerializer with HMAC-SHA1.

The signing key is the OIDC_TOKEN_ENCRYPTION_KEY (a Fernet base64url key, which is
valid arbitrary bytes for HMAC). The consuming code (phase 4 router) will pass the
key to StateCookieSerializer(key). Using the same key for both Fernet encryption and
HMAC signing is safe since itsdangerous derives separate subkeys via HMAC.

TTL defaults to 600 s (10 minutes) — long enough for a slow user at the IdP login page.
"""

from dataclasses import asdict, dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


@dataclass
class StateCookiePayload:
    state: str
    nonce: str
    code_verifier: str
    next_url: str = "/"


class StateCookieSerializer:
    """Signs/verifies the short-lived cookie holding OIDC state/nonce/code_verifier.

    TTL defaults to 600 s (10 minutes) — long enough for a slow user at the IdP.
    """

    COOKIE_NAME = "openrag_oidc_state"
    DEFAULT_TTL_SECONDS = 600

    def __init__(self, secret_key: str, salt: str = "openrag-oidc-state-v1"):
        self._serializer = URLSafeTimedSerializer(secret_key, salt=salt)

    def dumps(self, payload: StateCookiePayload) -> str:
        """Serialize and sign the payload, returning an opaque cookie value."""
        return self._serializer.dumps(asdict(payload))

    def loads(self, token: str, max_age: int = DEFAULT_TTL_SECONDS) -> StateCookiePayload:
        """Verify and deserialize the cookie value.

        Raises:
            ValueError: if the cookie is expired or the signature is invalid.
        """
        try:
            data = self._serializer.loads(token, max_age=max_age)
        except SignatureExpired as e:
            raise ValueError("OIDC state cookie expired") from e
        except BadSignature as e:
            raise ValueError("OIDC state cookie signature invalid") from e
        return StateCookiePayload(**data)
