"""Boot-time enforcement for credentials supplied by configuration.

The rules live in :mod:`core.secret_policy` so the application and the
pre-install environment-file checker use one policy table. Runtime checks are
scoped to OpenRAG; settings owned by another Compose service, such as Grafana,
do not prevent the API from starting.

``ALLOW_INSECURE_SECRETS=true`` downgrades violations to a warning. It is an
opt-out and never an opt-in, mirroring ``ALLOW_NO_AUTH``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

from core.secret_policy import (
    ALLOW_INSECURE_SECRETS_ENV_VAR,
    KNOWN_DEFAULT_SECRETS,
    MIN_SECRET_LENGTH,
    OPENRAG_SECRET_SCOPE,
    SECRET_SPECS,
    SecretSpec,
    is_known_default,
    secret_value_problem,
)
from core.utils.exceptions import ConfigError

logger = logging.getLogger(__name__)


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


def find_insecure_secrets(
    settings: Any = None,
    env: Mapping[str, str] | None = None,
    *,
    service_scope: str | None = OPENRAG_SECRET_SCOPE,
) -> list[str]:
    """Return problems for this service without exposing credential values.

    Set ``service_scope=None`` to inspect every service scope, as the
    environment-file checker does.
    """
    environ = os.environ if env is None else env
    problems: list[str] = []

    for spec in SECRET_SPECS:
        if service_scope is not None and spec.service_scope != service_scope:
            continue
        problem = secret_value_problem(spec, _resolve(spec, settings, environ), environ)
        if problem:
            problems.append(problem)

    return problems


def enforce_secret_policy(
    settings: Any = None,
    env: Mapping[str, str] | None = None,
    *,
    service_scope: str | None = OPENRAG_SECRET_SCOPE,
) -> None:
    """Refuse to start when this service has an unsafe configured secret.

    Raises:
        ConfigError: a credential is a published value or is too short, and
            ``ALLOW_INSECURE_SECRETS`` is not set to ``true``.
    """
    environ = os.environ if env is None else env
    problems = find_insecure_secrets(settings, environ, service_scope=service_scope)
    if not problems:
        return

    detail = " ".join(problems)
    if environ.get(ALLOW_INSECURE_SECRETS_ENV_VAR, "").strip().lower() == "true":
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
    "OPENRAG_SECRET_SCOPE",
    "SECRET_SPECS",
    "SecretSpec",
    "enforce_secret_policy",
    "find_insecure_secrets",
    "is_known_default",
    "secret_value_problem",
]
