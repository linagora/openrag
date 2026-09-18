"""Boot-time policy for credentials supplied by configuration.

Every value this module refuses is published somewhere in this repository — in
``.env.example``, in the chart's ``values.yaml``, in the documentation, or in a
test stack's compose file. They exist so the stack starts on a laptop without
anyone having to choose anything, and that is exactly why they must not reach a
deployment: the failure mode is copying, not choosing.

Two rules, both deliberately dull:

* **An exact-match denylist**, not an entropy heuristic. The condition being
  detected is "this is a value we published", so our own values match exactly
  and nothing else does. No false positives, nothing to tune.
* **One length floor**, applied only to the credentials whose format we define.
  An inference provider's API key or an identity provider's client secret is
  theirs to size, and ``EMPTY`` is the documented way to say an endpoint needs
  no credential at all — so those are checked against the denylist only.

Enforced once per process from :func:`core.config.get_settings`, which every
entrypoint crosses. ``ALLOW_INSECURE_SECRETS=true`` downgrades it to a warning;
it is an opt-out and never an opt-in, mirroring ``ALLOW_NO_AUTH``.

``infra/charts/openrag-stack/templates/secrets-env.yaml`` carries the same lists
for ``helm template`` time, because the chart can fail an install before it
happens and this cannot. A unit test fails the build when the two disagree.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.utils.exceptions import ConfigError

logger = logging.getLogger(__name__)

#: Opt-out flag. Never an opt-in: unset means enforced.
ALLOW_INSECURE_SECRETS_ENV_VAR = "ALLOW_INSECURE_SECRETS"

#: Minimum length for a credential whose format we define. Not a strength
#: target — a floor that catches a truncated paste or a one-word password.
#: Generated values are far longer (``secrets.token_hex(16)`` is 32 characters).
MIN_SECRET_LENGTH = 12

#: Values published by this project as examples, dev defaults or test fixtures,
#: matched case-insensitively after trimming. Grouped by where each is published
#: so the list stays auditable.
#:
#: ``EMPTY`` is deliberately absent: it is not a weak credential but the
#: project's sentinel for "this endpoint requires none", and the shipped value
#: for the bundled vLLM, embedder and reranker. It is still too short to pass
#: the length floor, so it cannot serve as a real credential where we set one.
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
        # The marker the example env files carry where a credential belongs, so
        # a copy that never went through scripts/gen_env.py is refused here
        # rather than starting something that merely looks configured.
        "__generate_me__",
    }
)


@dataclass(frozen=True)
class SecretSpec:
    """One credential the guard knows about.

    ``settings_path`` is set where the value can also arrive through
    ``conf/config.yaml``; most of these names are not mapped into
    :class:`~core.config.root.Settings` at all and are read from the
    environment only.

    ``enforce_min_length`` marks the credentials whose format this project
    defines, and is therefore the ones we get to size.
    """

    env_var: str
    purpose: str
    settings_path: str | None = None
    enforce_min_length: bool = False


#: The definitive list, and the answer to "what will be refused". Absence is
#: never a failure here: an unset ``AUTH_TOKEN`` is a supported development mode
#: and an unconfigured optional integration is normal. Requiredness is enforced
#: where an install happens — the chart fails closed at template time, and
#: compose fails closed on ``POSTGRES_PASSWORD``.
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
    ),
    SecretSpec(
        env_var="MINIO_SECRET_KEY",
        purpose="object-store secret key shared by MinIO and Milvus",
        enforce_min_length=True,
    ),
    SecretSpec(
        env_var="GRAFANA_ADMIN_PASSWORD",
        purpose="Grafana administrator password",
        enforce_min_length=True,
    ),
    SecretSpec(
        env_var="OIDC_CLIENT_SECRET",
        purpose="client secret issued by the identity provider",
    ),
    SecretSpec(
        env_var="OIDC_TOKEN_ENCRYPTION_KEY",
        purpose="Fernet key encrypting identity-provider tokens at rest",
    ),
    SecretSpec(
        env_var="HF_TOKEN",
        purpose="Hugging Face token used to pull gated model weights",
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


def _resolve(spec: SecretSpec, settings: Any, env: Mapping[str, str]) -> str:
    """Read a credential the way the application will read it.

    The environment wins: it is the layer an operator sets, and the layer
    ``load_dotenv`` has already merged by the time this runs. ``settings`` is
    the fallback for values that can also be written into ``conf/config.yaml``.
    Returns ``""`` when neither supplies one.
    """
    value = env.get(spec.env_var)
    if value:
        return value
    if spec.settings_path is None or settings is None:
        return ""
    current: Any = settings
    for part in spec.settings_path.split("."):
        current = getattr(current, part, None)
    return current if isinstance(current, str) else ""


def find_insecure_secrets(settings: Any = None, env: Mapping[str, str] | None = None) -> list[str]:
    """Return one human-readable problem per credential that violates policy.

    Never includes the offending value: these strings reach logs and, on a
    refused boot, a container's status output.
    """
    environ = os.environ if env is None else env
    problems: list[str] = []

    for spec in SECRET_SPECS:
        value = _resolve(spec, settings, environ).strip()
        if not value:
            # Unset, or set to whitespace, which every other layer reads as unset.
            continue
        if is_known_default(value):
            problems.append(
                f"{spec.env_var} is set to a value published by this project as an example "
                f"or development default. Supply a generated value ({spec.purpose})."
            )
        elif spec.enforce_min_length and len(value) < MIN_SECRET_LENGTH:
            problems.append(
                f"{spec.env_var} is shorter than {MIN_SECRET_LENGTH} characters. "
                f"Supply a generated value ({spec.purpose})."
            )

    return problems


def enforce_secret_policy(settings: Any = None, env: Mapping[str, str] | None = None) -> None:
    """Refuse to start when configuration carries a value we publish.

    Raises:
        ConfigError: a credential is a known published value, or is shorter than
            the floor, and ``ALLOW_INSECURE_SECRETS`` is not set to ``true``.
    """
    environ = os.environ if env is None else env
    problems = find_insecure_secrets(settings, environ)
    if not problems:
        return

    detail = " ".join(problems)
    if environ.get(ALLOW_INSECURE_SECRETS_ENV_VAR, "").strip().lower() == "true":
        # Warned on every boot, deliberately: a stack running with this flag
        # should never be able to claim it did not know.
        logger.warning("%s=true: starting with insecure configuration. %s", ALLOW_INSECURE_SECRETS_ENV_VAR, detail)
        return

    raise ConfigError(
        f"Refusing to start with insecure configuration. {detail} "
        f"Set {ALLOW_INSECURE_SECRETS_ENV_VAR}=true to override for a disposable "
        f"development or test stack.",
        code="INSECURE_SECRET_CONFIGURATION",
    )


__all__ = [
    "ALLOW_INSECURE_SECRETS_ENV_VAR",
    "KNOWN_DEFAULT_SECRETS",
    "MIN_SECRET_LENGTH",
    "SECRET_SPECS",
    "SecretSpec",
    "enforce_secret_policy",
    "find_insecure_secrets",
    "is_known_default",
]
