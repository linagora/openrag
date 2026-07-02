from __future__ import annotations


def test_redact_secrets_hides_known_secret_keys_without_false_token_matches():
    from core.utils.redaction import REDACTED_SECRET, redact_secrets

    payload = {
        "llm": {"api_key": "llm-secret", "model": "mistral"},
        "rdb": {"password": "db-secret", "host": "rdb"},
        "websearch": {"api_token": "search-secret", "max_tokens": 2048},
        "oidc_client_secret": "oidc-secret",
        "chainlit_auth_secret": "chainlit-secret",
        "nested": [{"token_encryption_key": "fernet-secret"}],
    }

    redacted = redact_secrets(payload)

    assert redacted["llm"]["api_key"] == REDACTED_SECRET
    assert redacted["rdb"]["password"] == REDACTED_SECRET
    assert redacted["websearch"]["api_token"] == REDACTED_SECRET
    assert redacted["websearch"]["max_tokens"] == 2048
    assert redacted["oidc_client_secret"] == REDACTED_SECRET
    assert redacted["chainlit_auth_secret"] == REDACTED_SECRET
    assert redacted["nested"][0]["token_encryption_key"] == REDACTED_SECRET
    assert payload["llm"]["api_key"] == "llm-secret"
