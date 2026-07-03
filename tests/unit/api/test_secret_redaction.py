from __future__ import annotations


def test_redact_secrets_fully_hides_known_secret_keys_without_false_token_matches():
    from core.utils.redaction import redact_secrets

    payload = {
        "llm": {"api_key": "sk-llm-secret", "model": "mistral"},
        "object_storage": {"access_key": "object-store-secret"},
        "rdb": {"password": "db-secret-value", "host": "rdb"},
        "websearch": {"api_token": "search-secret", "max_tokens": 2048},
        "oidc_client_secret": "oidc-secret",
        "chainlit_auth_secret": "chainlit-secret",
        "future": {
            "backend_secret": "backend-secret-value",
            "session_token": "session-token-value",
            "storage_access_key": "storage-access-key-value",
        },
        "nested": [
            {
                "private_key": "private-key-secret",
                "refresh_token": "refresh-token-secret",
                "signing_key": "signing-key-secret",
                "token_encryption_key": "fernet-secret",
            }
        ],
    }

    redacted = redact_secrets(payload)

    assert redacted["llm"]["api_key"] == "<redacted>"
    assert redacted["object_storage"]["access_key"] == "<redacted>"
    assert redacted["rdb"]["password"] == "<redacted>"
    assert redacted["websearch"]["api_token"] == "<redacted>"
    assert redacted["websearch"]["max_tokens"] == 2048
    assert redacted["oidc_client_secret"] == "<redacted>"
    assert redacted["chainlit_auth_secret"] == "<redacted>"
    assert redacted["future"]["backend_secret"] == "<redacted>"
    assert redacted["future"]["session_token"] == "<redacted>"
    assert redacted["future"]["storage_access_key"] == "<redacted>"
    assert redacted["nested"][0]["private_key"] == "<redacted>"
    assert redacted["nested"][0]["refresh_token"] == "<redacted>"
    assert redacted["nested"][0]["signing_key"] == "<redacted>"
    assert redacted["nested"][0]["token_encryption_key"] == "<redacted>"
    assert payload["llm"]["api_key"] == "sk-llm-secret"


def test_redact_secret_mapping_keeps_non_secret_endpoint_extra_shape():
    from core.utils.redaction import redact_secret_mapping

    redacted = redact_secret_mapping(
        {
            "api_key": "sk-top-level-secret",
            "implementation": "vllm",
            "auth": {"token": "nested-token"},
            "headers": [{"api_key": "hf-nested-secret"}],
            "temperature": 0.2,
            "enable_thinking": True,
        }
    )

    assert redacted == {
        "api_key": "sk-********",
        "implementation": "vllm",
        "auth": {"token": "nes********"},
        "headers": [{"api_key": "hf-********"}],
        "temperature": 0.2,
        "enable_thinking": True,
    }


def test_preserve_existing_secrets_accepts_prefix_masked_values():
    from core.utils.redaction import preserve_existing_secrets

    merged = preserve_existing_secrets(
        {
            "api_key": "sk-top-level-secret",
            "auth": {"token": "nested-token-secret"},
            "headers": [{"api_key": "hf-nested-secret"}],
        },
        {
            "api_key": "sk-********",
            "auth": {"token": "nes********"},
            "headers": [{"api_key": "hf-********"}],
        },
    )

    assert merged == {
        "api_key": "sk-top-level-secret",
        "auth": {"token": "nested-token-secret"},
        "headers": [{"api_key": "hf-nested-secret"}],
    }


def test_preserve_existing_secrets_clears_explicit_empty_secret_values():
    from core.utils.redaction import preserve_existing_secrets

    merged = preserve_existing_secrets(
        {
            "api_key": "stored-key",
            "auth": {"token": "nested-token"},
            "headers": [{"api_key": "nested-key"}],
        },
        {
            "implementation": "vllm",
            "api_key": "",
            "auth": {"token": None},
            "headers": [{"api_key": ""}],
        },
    )

    assert merged == {
        "implementation": "vllm",
        "auth": {},
        "headers": [{}],
    }


def test_preserve_existing_secrets_matches_list_items_by_non_secret_identity():
    from core.utils.redaction import preserve_existing_secrets

    merged = preserve_existing_secrets(
        {
            "providers": [
                {"name": "a", "api_key": "key-a"},
                {"name": "b", "api_key": "key-b"},
            ]
        },
        {
            "providers": [
                {"name": "b", "api_key": "<redacted>"},
            ]
        },
    )

    assert merged == {
        "providers": [
            {"name": "b", "api_key": "key-b"},
        ]
    }


def test_preserve_existing_secrets_drops_unmatched_list_placeholders_without_identity():
    from core.utils.redaction import preserve_existing_secrets

    merged = preserve_existing_secrets(
        {"headers": [{"api_key": "key-a"}, {"api_key": "key-b"}]},
        {"headers": [{"api_key": "<redacted>"}]},
    )

    assert merged == {"headers": [{}]}
