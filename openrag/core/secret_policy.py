"""Shared, dependency-free rules for configured credentials."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

OPENRAG_SECRET_SCOPE = "openrag"
COMPOSE_STORAGE_SECRET_SCOPE = "compose-storage"
ALLOW_INSECURE_SECRETS_ENV_VAR = "ALLOW_INSECURE_SECRETS"
OIDC_REQUIRED_ENV_VARS = (
    "OIDC_ENDPOINT",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_REDIRECT_URI",
    "OIDC_TOKEN_ENCRYPTION_KEY",
)


def parse_auth_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in ("token", "oidc"):
        raise ValueError("AUTH_MODE must be either 'token' or 'oidc'.")
    return mode


#: Minimum length for credentials whose format this project defines.
MIN_SECRET_LENGTH = 12

#: Values published by this project as examples, development defaults or test
#: fixtures. The marker is included so copying an example without generation is
#: refused at startup and by the environment-file checker.
KNOWN_DEFAULT_SECRETS: frozenset[str] = frozenset(
    {
        # infra/compose/.env.example and its documentation copies
        "or-openrag-1234",
        "sk-openrag-1234",
        "openrag-dev-secret",
        "minioadmin",
        "postgres",
        "replace-with-a-strong-password",
        # infra/charts/openrag-stack/values.yaml and the chart's own guard
        "sk-xxxx",
        "hf_xxxx",
        "change_me_strong_password",
        "root_password",
        # documentation site
        "sk-or-change-me",
        "change-me-the-secret-from-step-1",
        "your_secret_value",
        "your-api-key",
        "xxxxxxxxxxxxxxxxxxxxxxxx",
        # tests/integration/api/api_run
        "test-admin-token",
        "__generate_me__",
    }
)


@dataclass(frozen=True)
class SecretSpec:
    """A credential and the service whose startup policy owns it."""

    env_var: str
    purpose: str
    settings_path: str | None = None
    enforce_min_length: bool = False
    service_scope: str = OPENRAG_SECRET_SCOPE
    required_when: tuple[str, str] | None = None
    value_format: Literal["fernet"] | None = None


#: The canonical policy list used by application startup and gen_env.py.
SECRET_SPECS: tuple[SecretSpec, ...] = (
    SecretSpec(
        env_var="AUTH_TOKEN",
        purpose="bearer token that bootstraps the admin user and guards the API",
        enforce_min_length=True,
    ),
    SecretSpec(
        env_var="POSTGRES_PASSWORD",
        purpose="password for the application's Postgres role",
        settings_path="rdb.password",
        enforce_min_length=True,
    ),
    SecretSpec(
        env_var="CHAINLIT_AUTH_SECRET",
        purpose="signing secret for the chat interface's session cookie",
        enforce_min_length=True,
    ),
    SecretSpec(
        env_var="MINIO_ACCESS_KEY",
        purpose="object-store access key shared by MinIO and Milvus",
        service_scope=COMPOSE_STORAGE_SECRET_SCOPE,
    ),
    SecretSpec(
        env_var="MINIO_SECRET_KEY",
        purpose="object-store secret key shared by MinIO and Milvus",
        enforce_min_length=True,
        service_scope=COMPOSE_STORAGE_SECRET_SCOPE,
    ),
    SecretSpec(
        env_var="GRAFANA_ADMIN_PASSWORD",
        purpose="Grafana administrator password",
        enforce_min_length=True,
        service_scope="grafana",
    ),
    SecretSpec(
        env_var="OIDC_CLIENT_SECRET",
        purpose="client secret issued by the identity provider",
        required_when=("AUTH_MODE", "oidc"),
    ),
    SecretSpec(
        env_var="OIDC_TOKEN_ENCRYPTION_KEY",
        purpose="Fernet key encrypting identity-provider tokens at rest",
        required_when=("AUTH_MODE", "oidc"),
        value_format="fernet",
    ),
    SecretSpec(
        env_var="HF_TOKEN",
        purpose="Hugging Face token used by local audio transcription and model downloads",
    ),
    SecretSpec(
        env_var="API_KEY",
        purpose="credential for the chat LLM endpoint",
        settings_path="llm.api_key",
    ),
    SecretSpec(
        env_var="VLM_API_KEY",
        purpose="credential for the vision model endpoint",
        settings_path="vlm.api_key",
    ),
    SecretSpec(
        env_var="EMBEDDER_API_KEY",
        purpose="credential for the embedding endpoint",
        settings_path="embedder.api_key",
    ),
    SecretSpec(
        env_var="RERANKER_API_KEY",
        purpose="credential for the reranker endpoint",
        settings_path="reranker.api_key",
    ),
    SecretSpec(
        env_var="TRANSCRIBER_API_KEY",
        purpose="credential for the speech-to-text endpoint",
        settings_path="loader.transcriber.api_key",
    ),
    SecretSpec(
        env_var="WEBSEARCH_API_TOKEN",
        purpose="credential for the web-search provider",
        settings_path="websearch.api_token",
    ),
)


def is_known_default(value: str) -> bool:
    """Whether ``value`` is one this project publishes."""
    return value.strip().casefold() in KNOWN_DEFAULT_SECRETS


def _is_valid_fernet_key(value: str) -> bool:
    """Accept the same encoded key forms as cryptography.Fernet."""
    try:
        return len(base64.urlsafe_b64decode(value)) == 32
    except (binascii.Error, ValueError):
        return False


def secret_value_problem(
    spec: SecretSpec,
    value: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Describe a policy violation without including the credential value."""
    value = value.strip()
    if not value:
        if spec.required_when is not None:
            condition_var, condition_value = spec.required_when
            actual = (env or {}).get(condition_var, "token").strip().casefold()
            if actual == condition_value.casefold():
                return f"{spec.env_var} is required when {condition_var}={condition_value}. Supply a value."
        return None
    if is_known_default(value):
        return (
            f"{spec.env_var} is set to a value published by this project as an example "
            f"or development default. Supply a generated value ({spec.purpose})."
        )
    if spec.enforce_min_length and len(value) < MIN_SECRET_LENGTH:
        return (
            f"{spec.env_var} is shorter than {MIN_SECRET_LENGTH} characters. Supply a generated value ({spec.purpose})."
        )
    if spec.value_format == "fernet":
        validate_format = True
        if spec.required_when is not None:
            condition_var, condition_value = spec.required_when
            actual = (env or {}).get(condition_var, "token").strip().casefold()
            validate_format = actual == condition_value.casefold()
        if validate_format and not _is_valid_fernet_key(value):
            return f"{spec.env_var} must be a valid Fernet key. Supply a generated value."
    return None


__all__ = [
    "COMPOSE_STORAGE_SECRET_SCOPE",
    "KNOWN_DEFAULT_SECRETS",
    "MIN_SECRET_LENGTH",
    "OPENRAG_SECRET_SCOPE",
    "SECRET_SPECS",
    "SecretSpec",
    "is_known_default",
    "secret_value_problem",
]
