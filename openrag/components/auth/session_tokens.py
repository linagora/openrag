"""Session token utilities for OpenRAG OIDC sessions.

Opaque session tokens are issued at callback and stored hashed (SHA-256) in the DB.
IdP tokens (access_token, refresh_token) are encrypted with Fernet before storage.

The Fernet key is provided via the OIDC_TOKEN_ENCRYPTION_KEY environment variable.
Generate one with:
    python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
"""

import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken


def issue_session_token() -> tuple[str, str]:
    """Generate a new session token.

    Returns:
        (plaintext, sha256_hex) — the plaintext is set in the cookie,
        the hash is stored in the database.
    """
    plain = secrets.token_urlsafe(32)  # 43 chars, >= 256 bits entropy
    return plain, hash_session_token(plain)


def hash_session_token(token: str) -> str:
    """Return the SHA-256 hex digest of the session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fernet(key: str | bytes) -> Fernet:
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except Exception as e:
        raise ValueError(
            "OIDC_TOKEN_ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        ) from e


def encrypt_token(plaintext: str | None, key: str) -> bytes | None:
    """Encrypt a plaintext token string.

    Returns None if plaintext is None (refresh_token may be absent).
    """
    if plaintext is None:
        return None
    return _fernet(key).encrypt(plaintext.encode("utf-8"))


def decrypt_token(ciphertext: bytes | None, key: str) -> str | None:
    """Decrypt a Fernet-encrypted token.

    Returns None if ciphertext is None.
    Raises ValueError on key mismatch or data corruption.
    """
    if ciphertext is None:
        return None
    try:
        return _fernet(key).decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Failed to decrypt stored OIDC token — key mismatch or corruption") from e
