"""Encryption for callback credentials in the durable indexing queue."""

import os

from core.utils.exceptions import ConfigError
from cryptography.fernet import Fernet, MultiFernet


def queue_callback_cipher() -> MultiFernet:
    """Encrypt with the first dedicated key and decrypt with all configured keys."""
    keys = [key.strip() for key in os.environ.get("INDEXING_SECRETS_KEY", "").split(",") if key.strip()]
    if not keys:
        raise ConfigError("INDEXING_SECRETS_KEY is required for queued callback tokens.")
    fallback = os.environ.get("OIDC_TOKEN_ENCRYPTION_KEY")
    try:
        dedicated = [Fernet(key) for key in keys]
    except (ValueError, TypeError) as exc:
        raise ConfigError("INDEXING_SECRETS_KEY must contain valid Fernet keys.") from exc
    if fallback:
        # Invalid OIDC configuration should not prevent use of dedicated keys.
        try:
            dedicated.append(Fernet(fallback))
        except (ValueError, TypeError):
            pass
    return MultiFernet(dedicated)
