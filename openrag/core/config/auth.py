"""Authentication configuration — token mode + OIDC settings.

The legacy ``openrag/main.py`` did fail-fast validation of the OIDC
environment at module load and built request-bypass sets as
module-level frozensets in :mod:`api.middleware.auth`. Both pieces moved
here so the rules are configuration, not code:

* :class:`OIDCConfig` owns the OIDC schema **and** the env validator —
  :meth:`OIDCConfig.from_env` reads every ``OIDC_*`` / ``AUTH_MODE`` var,
  validates required-when-enabled, ``claim_source ∈ {id_token, userinfo}``,
  and that ``OIDC_CLAIM_MAPPING`` parses against the field whitelist.
  ``ServiceContainer.__init__`` invokes it so misconfiguration trips
  before the lifespan ever yields a request to the router stack.

* :class:`AuthBypassConfig` carries the three path policies the auth
  middleware consults on every request. Defaults match the legacy
  hardcoded sets exactly so the move is behaviour-preserving.
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# OIDC config + env validator
# ---------------------------------------------------------------------------


# Writable DB fields the OIDC claim mapping is allowed to populate.
# ``is_admin`` / ``external_user_id`` / ``file_quota`` / ``token`` are
# either identity-defining or privilege-escalation vectors and must
# never be writable through claims.
_OIDC_CLAIM_MAPPING_ALLOWED_FIELDS: frozenset[str] = frozenset({"display_name", "email"})


def _parse_oidc_claim_mapping(raw: str) -> dict[str, str]:
    """Parse ``OIDC_CLAIM_MAPPING`` (CSV of ``db_field:claim`` pairs).

    Raises ``RuntimeError`` on any malformed or non-whitelisted entry so
    operator typos fail at startup rather than silently at the next
    login. The runtime path in :class:`AuthService` re-parses the same
    string but drops malformed entries rather than raising — by the
    time a request reaches it, this validator has already vetted the
    value at container construction.
    """
    if not raw:
        return {}
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise RuntimeError(f"Invalid OIDC_CLAIM_MAPPING entry {pair!r}: expected 'db_field:claim'")
        db_field, claim = pair.split(":", 1)
        db_field = db_field.strip()
        claim = claim.strip()
        if db_field not in _OIDC_CLAIM_MAPPING_ALLOWED_FIELDS:
            raise RuntimeError(
                f"OIDC_CLAIM_MAPPING db_field {db_field!r} is not writable "
                f"(allowed: {sorted(_OIDC_CLAIM_MAPPING_ALLOWED_FIELDS)})"
            )
        if not claim:
            raise RuntimeError(f"OIDC_CLAIM_MAPPING entry for {db_field!r} has empty claim name")
        mapping[db_field] = claim
    return mapping


# Default Keycloak group → (partition, role) pattern, applied to each group
# *after* ``OIDC_GROUP_PREFIX`` is stripped. For a group ``/openrag/alpha/editor``
# with the default prefix ``/openrag/`` it matches the remainder ``alpha/editor``
# and captures ``("alpha", "editor")`` — group 1 = partition, group 2 = role.
# The trailing ``$`` anchor is a deliberate refinement over the strategy doc so
# trailing garbage cannot accidentally match (operators may override it).
_DEFAULT_OIDC_GROUP_PATTERN: str = r"(.+)/(owner|editor|viewer)$"


def _compile_group_pattern(raw: str) -> re.Pattern[str]:
    """Compile ``OIDC_GROUP_PATTERN`` and assert it captures two groups.

    Raises ``RuntimeError`` on an invalid regex, or one exposing fewer than
    two capture groups (partition + role), so an operator typo trips at
    startup rather than silently mapping zero groups at the next login. Run
    unconditionally (like :func:`_parse_oidc_claim_mapping`) so a bad pattern
    fails even before ``OIDC_CLAIM_GROUPS`` is set to switch the feature on.
    """
    try:
        pattern = re.compile(raw)
    except re.error as e:
        raise RuntimeError(f"Invalid OIDC_GROUP_PATTERN {raw!r}: {e}") from e
    if pattern.groups < 2:
        raise RuntimeError(
            f"OIDC_GROUP_PATTERN {raw!r} must expose at least two capture groups "
            f"(partition, role); got {pattern.groups}"
        )
    return pattern


class OIDCConfig(BaseModel):
    """OIDC configuration for Keycloak / external IdP integration.

    All fields are optional — if OIDC is not enabled, this section is ignored.
    Populated from environment variables (OIDC_ENDPOINT, OIDC_CLIENT_ID, etc.).
    """

    enabled: bool = False
    issuer_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    scopes: str = "openid email profile offline_access"
    token_encryption_key: str = ""
    claim_source: str = "id_token"
    claim_mapping: str = ""
    post_logout_redirect_uri: str = ""
    # When True, an unknown ``sub`` at callback time provisions a
    # non-admin user on the fly from the ID-token claims. Default keeps
    # the strict "admin pre-creates every user" policy.
    auto_provision_login: bool = False

    # --- Group → partition membership sync (Phase 15) ---------------------
    # Empty ``claim_groups`` disables the feature entirely, so every existing
    # deployment is unaffected. When set, the named claim is read from the
    # verified ID token at callback time, each group is matched against
    # ``group_pattern`` (after ``group_prefix`` is stripped) to yield a
    # (partition, role) pair, and the user's memberships are reconciled.
    # ``is_admin`` is never derived from claims — that hard boundary (see the
    # claim-mapping whitelist above) is preserved.
    claim_groups: str = ""
    group_prefix: str = "/openrag/"
    group_pattern: str = _DEFAULT_OIDC_GROUP_PATTERN
    # When True, memberships absent from the token are removed (the IdP is the
    # sole source of truth). Default keeps manually-granted memberships.
    group_sync_prune: bool = False

    # The five env vars required when AUTH_MODE=oidc; unset/empty values
    # trip the validator with a single error listing every missing key.
    _REQUIRED_WHEN_ENABLED: ClassVar[tuple[str, ...]] = (
        "OIDC_ENDPOINT",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI",
        "OIDC_TOKEN_ENCRYPTION_KEY",
    )
    _VALID_CLAIM_SOURCES: ClassVar[frozenset[str]] = frozenset({"id_token", "userinfo"})

    @classmethod
    def from_env(cls) -> OIDCConfig:
        """Build + validate an :class:`OIDCConfig` from the process env.

        Reads ``AUTH_MODE`` to decide ``enabled``, then every
        ``OIDC_*`` var. When OIDC is enabled, the required-vars set must
        be fully populated; in either mode, ``OIDC_CLAIM_SOURCE`` must
        be one of ``id_token`` / ``userinfo`` and
        ``OIDC_CLAIM_MAPPING`` must parse cleanly against the writable
        whitelist. Any violation raises ``RuntimeError`` so the
        composition root surfaces misconfiguration before the app
        starts serving.
        """
        auth_mode = os.getenv("AUTH_MODE", "token").strip().lower()
        if auth_mode not in ("token", "oidc"):
            raise RuntimeError(f"Invalid AUTH_MODE={auth_mode!r}. Expected 'token' or 'oidc'.")

        enabled = auth_mode == "oidc"

        env_values = {
            "OIDC_ENDPOINT": os.getenv("OIDC_ENDPOINT"),
            "OIDC_CLIENT_ID": os.getenv("OIDC_CLIENT_ID"),
            "OIDC_CLIENT_SECRET": os.getenv("OIDC_CLIENT_SECRET"),
            "OIDC_REDIRECT_URI": os.getenv("OIDC_REDIRECT_URI"),
            "OIDC_TOKEN_ENCRYPTION_KEY": os.getenv("OIDC_TOKEN_ENCRYPTION_KEY"),
        }
        claim_source = os.getenv("OIDC_CLAIM_SOURCE", "id_token").strip().lower()
        claim_mapping = os.getenv("OIDC_CLAIM_MAPPING", "").strip()

        if enabled:
            missing = [name for name, val in env_values.items() if not val]
            if missing:
                raise RuntimeError(
                    "AUTH_MODE=oidc but the following env vars are missing or empty: " + ", ".join(missing)
                )
            if claim_source not in cls._VALID_CLAIM_SOURCES:
                raise RuntimeError(
                    f"Invalid OIDC_CLAIM_SOURCE={claim_source!r}. Expected one of {sorted(cls._VALID_CLAIM_SOURCES)}."
                )

        # Parse-and-discard: raises on malformed entries regardless of
        # whether OIDC is enabled, because a misconfigured claim mapping
        # on an operator's box would silently start ignoring claims
        # the moment they flipped AUTH_MODE=oidc.
        _parse_oidc_claim_mapping(claim_mapping)

        # Same parse-and-discard philosophy for the group pattern: validate
        # the regex compiles and exposes two capture groups regardless of
        # whether group sync is currently active.
        group_pattern = os.getenv("OIDC_GROUP_PATTERN", _DEFAULT_OIDC_GROUP_PATTERN)
        _compile_group_pattern(group_pattern)

        return cls(
            enabled=enabled,
            issuer_url=env_values["OIDC_ENDPOINT"] or "",
            client_id=env_values["OIDC_CLIENT_ID"] or "",
            client_secret=env_values["OIDC_CLIENT_SECRET"] or "",
            redirect_uri=env_values["OIDC_REDIRECT_URI"] or "",
            scopes=os.getenv("OIDC_SCOPES", "openid email profile offline_access"),
            token_encryption_key=env_values["OIDC_TOKEN_ENCRYPTION_KEY"] or "",
            claim_source=claim_source,
            claim_mapping=claim_mapping,
            post_logout_redirect_uri=os.getenv("OIDC_POST_LOGOUT_REDIRECT_URI", "") or "",
            auto_provision_login=os.getenv("OIDC_AUTO_PROVISION_LOGIN", "false").strip().lower() == "true",
            claim_groups=os.getenv("OIDC_CLAIM_GROUPS", "").strip(),
            group_prefix=os.getenv("OIDC_GROUP_PREFIX", "/openrag/"),
            group_pattern=group_pattern,
            group_sync_prune=os.getenv("OIDC_GROUP_SYNC_PRUNE", "false").strip().lower() == "true",
        )


# ---------------------------------------------------------------------------
# AuthMiddleware bypass policy
# ---------------------------------------------------------------------------


# Paths the auth middleware exits early on. Includes the OpenAPI docs,
# health probes, and the OIDC callback endpoints (which can't require
# auth because they ARE the way users get authenticated).
DEFAULT_BYPASS_PATHS: tuple[str, ...] = (
    "/docs",
    "/openapi.json",
    "/redoc",
    "/health_check",
    "/version",
    "/auth/login",
    "/auth/callback",
    "/auth/backchannel-logout",
    "/auth/logout",
)

# REST API prefixes. Unauthenticated requests here get JSON 401/403,
# never an HTML redirect to /auth/login (would break programmatic
# clients that follow redirects).
DEFAULT_API_PREFIXES: tuple[str, ...] = (
    "/v1/",
    "/indexer/",
    "/search/",
    "/users/",
    "/partition/",
    "/workspaces/",
    "/queue/",
    "/extract/",
    "/actors/",
    "/monitoring/",
    "/tools/",
)

# Browser-facing path prefixes. Unauthenticated access in oidc mode
# triggers a 302 redirect to /auth/login (so the user lands in the
# IdP flow instead of staring at a 401 JSON blob).
DEFAULT_UI_PATH_PREFIXES: tuple[str, ...] = ("/static",)


class AuthBypassConfig(BaseModel):
    """Path policy consulted by :class:`AuthMiddleware` per request.

    Defaults match the legacy hardcoded module-level sets so the Phase
    10C move is behaviour-preserving. Operators that need to tighten
    or widen any list construct an :class:`AuthBypassConfig` with
    overrides and hand it to the middleware at registration time.
    """

    bypass_paths: tuple[str, ...] = DEFAULT_BYPASS_PATHS
    api_prefixes: tuple[str, ...] = DEFAULT_API_PREFIXES
    ui_path_prefixes: tuple[str, ...] = DEFAULT_UI_PATH_PREFIXES
