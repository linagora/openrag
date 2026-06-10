"""AuthService — OIDC flow + auth-policy orchestration (Phase 8A.1).

Business logic extracted from ``routers/auth.py`` and the auth helpers in
``routers/utils.py``. The router keeps HTTP transport only (cookies,
redirects, JSON error shaping, the ``AUTH_MODE`` gate); every decision
lives here and is unit-testable with fake repos / OIDC client.

Compared to the legacy router this service talks to the Phase 7 domain
repositories (``UserRepository``, ``OIDCSessionRepository``) instead of
the Ray ``vectordb`` actor, so it deals in :class:`User` /
:class:`OIDCSession` models rather than ad-hoc dicts.

The cryptographic / cookie primitives (``OIDCClient``, the state-cookie
serializer, Fernet token (de)encryption, opaque session-token issuance)
come from ``services.auth`` — the infrastructure adapters for OIDC.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode, urlparse

from core.models.user import OIDCSession, User
from core.utils.exceptions import AuthError, OpenRAGError
from core.utils.logging import get_logger, mask_email
from services.auth import (
    OIDCClient,
    StateCookiePayload,
    StateCookieSerializer,
    decrypt_token,
    encrypt_token,
    hash_session_token,
    issue_session_token,
)

if TYPE_CHECKING:
    from core.config.auth import OIDCConfig
    from core.ports.oidc_session_repo import OIDCSessionRepository
    from core.ports.partition_membership_repo import PartitionMembershipRepository
    from core.ports.user_repo import UserRepository

logger = get_logger()

SESSION_COOKIE_NAME = "openrag_session"


class OIDCFlowError(OpenRAGError):
    """Raised for any recoverable failure inside the OIDC flow.

    Carries the exact ``status_code`` the legacy router used so the thin
    router can reproduce the previous HTTP responses verbatim.
    ``error_description`` is only set for back-channel logout, where the
    OIDC spec wants an ``error_description`` field in the JSON body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        error_description: str | None = None,
    ) -> None:
        super().__init__(message, code="OIDC_FLOW_ERROR", status_code=status_code)
        self.error_description = error_description


@dataclass
class LoginRedirect:
    """Everything the router needs to start the Authorization Code flow."""

    authorization_url: str
    state_cookie_name: str
    state_cookie_value: str
    state_cookie_max_age: int


@dataclass
class CallbackResult:
    """Everything the router needs to finish login (set session cookie)."""

    session_cookie_name: str
    session_cookie_value: str  # plaintext — only the SHA-256 hash is stored
    session_cookie_max_age: int
    next_url: str


def _utcnow() -> datetime:
    """Naive local ``now`` — matches the DB columns.

    The ``oidc_sessions`` timestamp columns are ``TIMESTAMP WITHOUT TIME
    ZONE`` and every read site compares against ``datetime.now()``; using a
    tz-aware value here would make freshly-issued sessions look pre-expired
    on non-UTC hosts (and asyncpg refuses tz-aware against tz-naive).
    """
    return datetime.now()


class AuthService:
    """Owns the OIDC Authorization-Code + PKCE flow and auth policy."""

    ROLE_HIERARCHY: dict[str, int] = {"viewer": 1, "editor": 2, "owner": 3}

    # Defence-in-depth: an IdP claim mapping may only ever write these two
    # columns. Mirrors the startup validator and the repo whitelist.
    _OIDC_CLAIM_MAPPING_ALLOWED_FIELDS = frozenset({"display_name", "email"})

    def __init__(
        self,
        *,
        user_repo: UserRepository,
        oidc_session_repo: OIDCSessionRepository,
        membership_repo: PartitionMembershipRepository,
        oidc_client: OIDCClient | None,
        config: OIDCConfig,
    ) -> None:
        self._user_repo = user_repo
        self._oidc_session_repo = oidc_session_repo
        # Retained for the role/membership helpers that later phases will
        # route through this service (8B onward); the OIDC flow itself
        # does not touch it.
        self._membership_repo = membership_repo
        self._oidc_client = oidc_client
        self._config = config

    # ------------------------------------------------------------------
    # OIDC flow
    # ------------------------------------------------------------------

    async def start_oidc_login(self, next_url: str | None) -> LoginRedirect:
        """Generate PKCE + state/nonce and build the IdP authorization URL."""
        client = self._require_client()
        state, nonce = OIDCClient.generate_state_and_nonce()
        code_verifier, code_challenge = OIDCClient.generate_pkce_pair()

        try:
            auth_url = await client.build_authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=code_challenge,
            )
        except Exception as e:
            logger.error(f"Failed to build OIDC authorization URL: {e}")
            raise OIDCFlowError(
                "OIDC discovery failed — see server logs.",
                status_code=502,
            ) from e

        payload = StateCookiePayload(
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            next_url=self.sanitize_next_url(next_url),
        )
        cookie_value = self._state_serializer().dumps(payload)
        return LoginRedirect(
            authorization_url=auth_url,
            state_cookie_name=StateCookieSerializer.COOKIE_NAME,
            state_cookie_value=cookie_value,
            state_cookie_max_age=StateCookieSerializer.DEFAULT_TTL_SECONDS,
        )

    async def handle_oidc_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        state_cookie_raw: str | None,
    ) -> CallbackResult:
        """Validate the IdP redirect, resolve the user, create a session."""
        client = self._require_client()

        if not code or not state:
            raise OIDCFlowError("Missing 'code' or 'state' query parameter.")
        if not state_cookie_raw:
            raise OIDCFlowError("OIDC state cookie missing.")

        try:
            payload = self._state_serializer().loads(state_cookie_raw)
        except ValueError as e:
            logger.warning(f"Invalid OIDC state cookie: {e}")
            raise OIDCFlowError("Invalid or expired OIDC state cookie.") from e

        # CSRF: the query ``state`` must match the signed cookie.
        if state != payload.state:
            logger.warning("OIDC state mismatch between query and cookie")
            raise OIDCFlowError("OIDC state mismatch.")

        try:
            bundle = await client.exchange_code(
                code=code,
                code_verifier=payload.code_verifier,
                expected_nonce=payload.nonce,
            )
        except Exception as e:
            # Generic message — IdP URLs / internals must not leak via HTTP.
            logger.exception("OIDC code exchange failed")
            raise OIDCFlowError("OIDC code exchange failed") from e

        sub = bundle.claims.get("sub")
        if not sub:
            raise OIDCFlowError("ID token missing 'sub' claim.")

        user = await self._resolve_user(sub, bundle.claims)
        user = await self._sync_auto_provisioned(user, sub, bundle.claims)
        user = await self._apply_claim_mapping(user, bundle)

        now = _utcnow()
        expires_in = max(int(bundle.expires_in or 0), 60)
        access_token_expires_at = now + timedelta(seconds=expires_in)
        session_expires_at = now + timedelta(days=7) if bundle.refresh_token else access_token_expires_at

        plain, token_hash = issue_session_token()
        key = self._config.token_encryption_key
        await self._oidc_session_repo.create_session(
            OIDCSession(
                session_token_hash=token_hash,
                user_id=user.id,
                sub=sub,
                sid=bundle.claims.get("sid"),
                id_token_encrypted=encrypt_token(bundle.id_token, key=key),
                access_token_encrypted=encrypt_token(bundle.access_token, key=key),
                refresh_token_encrypted=encrypt_token(bundle.refresh_token, key=key),
                access_token_expires_at=access_token_expires_at,
                session_expires_at=session_expires_at,
                created_at=now,
            )
        )

        next_url = self.sanitize_next_url(payload.next_url)
        max_age = max(int((session_expires_at - now).total_seconds()), 1)
        logger.info(f"OIDC login success — user_id={user.id}, sid={bundle.claims.get('sid')!r}, next={next_url!r}")
        return CallbackResult(
            session_cookie_name=SESSION_COOKIE_NAME,
            session_cookie_value=plain,
            session_cookie_max_age=max_age,
            next_url=next_url,
        )

    async def handle_backchannel_logout(self, logout_token: str) -> int:
        """Verify an IdP logout token and revoke the named session(s)."""
        client = self._require_client()
        try:
            claims = await client.verify_logout_token(logout_token)
        except ValueError as e:
            logger.warning(f"Invalid back-channel logout token: {e}")
            raise OIDCFlowError(str(e), error_description=str(e)) from e
        except Exception as e:
            logger.warning(f"Back-channel logout token verification failed: {e}")
            raise OIDCFlowError("invalid_request") from e

        if claims.sid:
            count = await self._oidc_session_repo.revoke_by_sid(claims.sid)
            logger.info(f"Back-channel logout revoked sessions — sid={claims.sid!r}, count={count}")
            return count

        # Policy: sid-less logout tokens are out of scope (still 200 to the
        # IdP so it doesn't retry).
        logger.warning(
            f"Received sid-less back-channel logout token — not supported; "
            f"ignoring per implementation policy (sub={claims.sub!r})"
        )
        return 0

    async def logout(self, session_cookie_value: str | None) -> str | None:
        """Revoke the local session and build the IdP end-session redirect.

        Returns the redirect target, or ``None`` when neither an IdP
        ``end_session_endpoint`` nor a configured post-logout URL exists
        (the router then just confirms the logout in place).
        """
        client = self._require_client()

        id_token_hint: str | None = None
        if session_cookie_value:
            session = await self._oidc_session_repo.get_by_token_hash(
                hash_session_token(session_cookie_value),
            )
            if session:
                if session.id_token_encrypted:
                    try:
                        id_token_hint = decrypt_token(
                            session.id_token_encrypted,
                            key=self._config.token_encryption_key,
                        )
                    except ValueError as e:
                        logger.warning(f"Failed to decrypt id_token for logout: {e}")
                try:
                    await self._oidc_session_repo.revoke_session(session.id)
                except Exception as e:
                    logger.warning(f"Failed to revoke oidc_session during logout: {e}")

        local_target = self._config.post_logout_redirect_uri or None
        redirect_target: str | None = local_target
        try:
            meta = await client.discover()
            end_session = meta.get("end_session_endpoint")
            if end_session:
                params: dict[str, str] = {"client_id": self._config.client_id}
                if local_target:
                    params["post_logout_redirect_uri"] = local_target
                if id_token_hint:
                    params["id_token_hint"] = id_token_hint
                redirect_target = f"{end_session}?{urlencode(params)}"
        except Exception as e:
            logger.warning(f"OIDC discovery failed during logout, skipping IdP redirect: {e}")

        return redirect_target

    # ------------------------------------------------------------------
    # Request authentication helpers
    # ------------------------------------------------------------------

    async def get_user_for_request(self, user_id: int) -> dict[str, Any] | None:
        user = await self._user_repo.get_user(user_id)
        return self._user_to_request_dict(user) if user else None

    async def get_user_by_token_for_request(self, token: str) -> dict[str, Any] | None:
        user = await self._user_repo.get_user_by_token(hash_session_token(token))
        return self._user_to_request_dict(user) if user else None

    async def list_user_partitions_for_request(self, user_id: int) -> list[dict[str, Any]]:
        memberships = await self._membership_repo.list_user_partitions(user_id)
        return [
            {
                "partition": membership.partition,
                "role": membership.role.value,
                "created_at": membership.added_at.isoformat() if membership.added_at else None,
            }
            for membership in memberships
        ]

    async def get_oidc_session_by_token_for_request(self, token: str) -> dict[str, Any] | None:
        session = await self._oidc_session_repo.get_by_token_hash(hash_session_token(token))
        return self._oidc_session_to_request_dict(session) if session else None

    async def get_oidc_session_by_id_for_request(self, session_id: int) -> dict[str, Any] | None:
        session = await self._oidc_session_repo.get_by_id(session_id)
        return self._oidc_session_to_request_dict(session) if session else None

    async def update_oidc_session_tokens_for_request(
        self,
        *,
        session_id: int,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at: datetime,
    ) -> None:
        updates: dict[str, Any] = {
            "access_token_encrypted": access_token_encrypted,
            "access_token_expires_at": access_token_expires_at,
            "last_refresh_at": _utcnow(),
        }
        if refresh_token_encrypted is not None:
            updates["refresh_token_encrypted"] = refresh_token_encrypted
        session = await self._oidc_session_repo.update_session(session_id, **updates)
        if session is None:
            raise ValueError(f"oidc_session id={session_id} does not exist")

    async def revoke_oidc_session_by_id_for_request(self, session_id: int) -> None:
        await self._oidc_session_repo.revoke_session(session_id)

    async def refresh_session_if_needed(self, *, session: dict[str, Any], enc_key: str) -> dict[str, Any] | None:
        """Refresh the IdP access token when it is near expiry.

        Thin seam over :func:`services.auth.refresh.refresh_session_if_needed`,
        passing ``self`` as the auth-service the helper calls back into. Keeping
        it on the orchestrator lets the middleware reach refresh through the same
        ``AuthService`` it already uses for every other session operation —
        the API layer never imports the services helper directly.
        """
        from services.auth.refresh import refresh_session_if_needed as _refresh

        return await _refresh(session=session, enc_key=enc_key, auth_service=self)

    # ------------------------------------------------------------------
    # Auth policy — pure helpers (no I/O)
    # ------------------------------------------------------------------

    @staticmethod
    def _uget(user: Any, key: str, default: Any = None) -> Any:
        """Read a user attribute whether ``user`` is a dict or :class:`User`.

        The legacy middleware binds ``request.state.user`` as a dict; the
        new repos return :class:`User`. Both shapes flow through these
        helpers during the shim period.
        """
        if isinstance(user, dict):
            return user.get(key, default)
        return getattr(user, key, default)

    @staticmethod
    def _user_to_request_dict(user: User) -> dict[str, Any]:
        return {
            "id": user.id,
            "display_name": user.display_name,
            "external_user_id": user.external_user_id,
            "email": user.email,
            "is_admin": user.is_admin,
            "file_quota": user.file_quota,
            "file_count": user.file_count,
            "memberships": [
                {
                    "partition": membership.partition,
                    "role": membership.role.value,
                    "added_at": membership.added_at.isoformat() if membership.added_at else None,
                }
                for membership in user.partitions
            ],
        }

    @staticmethod
    def _oidc_session_to_request_dict(session: OIDCSession) -> dict[str, Any]:
        return {
            "id": session.id,
            "user_id": session.user_id,
            "sub": session.sub,
            "sid": session.sid,
            "id_token_encrypted": session.id_token_encrypted,
            "access_token_encrypted": session.access_token_encrypted,
            "refresh_token_encrypted": session.refresh_token_encrypted,
            "access_token_expires_at": session.access_token_expires_at,
            "session_expires_at": session.session_expires_at,
            "created_at": session.created_at,
            "last_refresh_at": session.last_refresh_at,
            "revoked_at": session.revoked_at,
        }

    @classmethod
    def require_admin(cls, user: Any) -> Any:
        """Raise :class:`AuthError` (403) unless the user is an admin."""
        if not user or not cls._uget(user, "is_admin", False):
            raise AuthError("Admin privileges required", status_code=403)
        return user

    @classmethod
    def check_partition_access(
        cls,
        *,
        user: Any,
        partition: str,
        user_partitions: list[dict[str, Any]],
        required_role: str,
        super_admin_mode: bool = False,
    ) -> bool:
        """Pure port of ``ensure_partition_role``.

        Unlike the legacy helper this does **not** probe the vector DB for
        partition existence — the "unknown partition is allowed" branch was
        an I/O side effect. Callers that still need it must validate
        existence separately; here, absence of a membership for an
        otherwise-known partition is a 403.
        """
        if super_admin_mode and cls._uget(user, "is_admin", False):
            return True

        membership = next(
            (p for p in user_partitions if p.get("partition") == partition),
            None,
        )
        if not membership:
            raise AuthError(
                f"Access to partition '{partition}' forbidden",
                status_code=403,
            )

        user_role = membership.get("role")
        if user_role not in cls.ROLE_HIERARCHY:
            raise AuthError(
                f"Access to partition '{partition}' forbidden",
                status_code=403,
            )
        if cls.ROLE_HIERARCHY[user_role] < cls.ROLE_HIERARCHY[required_role]:
            raise AuthError(
                f"{required_role.capitalize()} role required for partition '{partition}'",
                status_code=403,
            )
        return True

    @classmethod
    def validate_file_quota(
        cls,
        user: Any,
        *,
        pending_task_count: int,
        default_quota: int,
    ) -> None:
        """Pure quota check (the pending-task count is supplied by the caller).

        Quota semantics are unchanged from ``check_user_file_quota``:
        admins bypass; ``default_quota < 0`` disables; ``file_quota`` of
        ``None`` falls back to the default; ``< 0`` means unlimited.
        """
        if cls._uget(user, "is_admin", False):
            return
        if default_quota < 0:
            return

        user_quota = cls._uget(user, "file_quota")
        if user_quota is None:
            user_quota = default_quota
        if user_quota < 0:
            return

        indexed_count = cls._uget(user, "file_count", 0) or 0
        total = indexed_count + pending_task_count
        if total >= user_quota:
            raise OpenRAGError(
                f"File quota exceeded. You have {indexed_count} indexed files "
                f"and {pending_task_count} pending tasks. Limit: {user_quota}",
                code="FILE_QUOTA_EXCEEDED",
                status_code=403,
            )

    def sanitize_next_url(self, next_url: str | None) -> str:
        """Block open redirects, header injection, and protocol-relative paths.

        Accept a same-origin relative path (``/...`` but not ``//...`` and
        not ``/\\...``) or an absolute URL whose origin is explicitly
        whitelisted; fall back to ``/`` otherwise.

        Defenses applied beyond the basic prefix check:
          * any CR / LF / NUL byte triggers fallback (HTTP response splitting
            and header injection via ``Location: ...``).
          * a path-relative value whose second character is ``/`` or ``\\``
            is rejected because Chrome / Edge parse ``/\\evil.com`` as a
            host-relative URL whose host is ``evil.com``.
          * any apparent path is re-parsed and only accepted when
            ``urlsplit`` confirms an empty ``scheme`` and ``netloc``.
        """
        if not next_url:
            return "/"
        if any(ch in next_url for ch in ("\r", "\n", "\x00")):
            return "/"
        if next_url.startswith("/"):
            if len(next_url) > 1 and next_url[1] in ("/", "\\"):
                return "/"
            parsed = urlparse(next_url)
            if parsed.scheme or parsed.netloc:
                return "/"
            return next_url
        parsed = urlparse(next_url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin in self._allowed_next_origins():
                return next_url
        return "/"

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_client(self) -> OIDCClient:
        if self._oidc_client is None:
            raise OIDCFlowError("OIDC is not configured.", status_code=400)
        return self._oidc_client

    def _state_serializer(self) -> StateCookieSerializer:
        return StateCookieSerializer(secret_key=self._config.token_encryption_key)

    @staticmethod
    def _allowed_next_origins() -> set[str]:
        """Origins accepted as post-login redirect targets.

        Mirrors the CORS allowlist: localhost dev ports plus
        ``INDEXERUI_URL`` so the separately-served indexer-ui can receive
        the user back after the flow.
        """
        origins = {"http://localhost:3042", "http://localhost:5173"}
        indexer_ui = os.getenv("INDEXERUI_URL")
        if indexer_ui:
            origins.add(indexer_ui.rstrip("/"))
        return origins

    @staticmethod
    def _display_name_from_claims(claims: dict[str, Any], sub: str) -> str:
        """Pick a printable display name from the standard OIDC claims."""
        for key in ("name", "preferred_username"):
            value = claims.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        given = claims.get("given_name") or ""
        family = claims.get("family_name") or ""
        composed = f"{given} {family}".strip()
        if composed:
            return composed
        return f"oidc-{sub[:8]}"

    @classmethod
    def _parse_claim_mapping(cls, raw: str) -> dict[str, str]:
        """Parse ``OIDC_CLAIM_MAPPING`` (CSV of ``db_field:claim`` pairs).

        Non-whitelisted / malformed entries are dropped silently — the
        hard-failure path is the startup validator; at login time we log
        and continue rather than break the flow.
        """
        raw = (raw or "").strip()
        if not raw:
            return {}
        mapping: dict[str, str] = {}
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            db_field, claim = pair.split(":", 1)
            db_field = db_field.strip()
            claim = claim.strip()
            if db_field not in cls._OIDC_CLAIM_MAPPING_ALLOWED_FIELDS or not claim:
                continue
            mapping[db_field] = claim
        return mapping

    async def _resolve_user(self, sub: str, claims: dict[str, Any]) -> User:
        """Look the user up by ``sub``; auto-provision if configured."""
        user = await self._user_repo.get_user_by_external_id(sub)
        if user is not None:
            return user

        if not self._config.auto_provision_login:
            logger.warning(f"OIDC login rejected — user not registered (sub={sub!r})")
            raise OIDCFlowError("User not registered", status_code=403)

        display_name = self._display_name_from_claims(claims, sub)
        email = claims.get("email")
        try:
            user = await self._user_repo.create_user(
                User(
                    display_name=display_name,
                    external_user_id=sub,
                    email=email if isinstance(email, str) and email.strip() else None,
                    is_admin=False,
                )
            )
        except Exception as e:
            # Concurrent first-login race or DB failure. Re-read by sub first;
            # if an email collision caused the insert failure, return an
            # actionable conflict instead of an opaque 500.
            logger.exception(f"OIDC auto-provisioning failed for sub={sub!r}: {e}")
            user = await self._user_repo.get_user_by_external_id(sub)
            if user is None:
                if isinstance(email, str) and email.strip():
                    existing = await self._user_repo.get_user_by_email(email)
                    if existing is not None:
                        logger.error(
                            f"OIDC auto-provisioning blocked for sub={sub!r}: an account with email "
                            f"{mask_email(email)} already exists under a different identity. Set that "
                            f"user's external_user_id to this sub to allow login."
                        )
                        raise OIDCFlowError(
                            "An account with this email already exists. Ask your administrator to "
                            "link it to your identity provider login.",
                            status_code=409,
                        ) from e
                raise OIDCFlowError("Failed to provision user", status_code=500) from e
        else:
            logger.info(f"OIDC user auto-provisioned (id={user.id}, sub={sub!r})")
        return user

    async def _sync_auto_provisioned(
        self,
        user: User,
        sub: str,
        claims: dict[str, Any],
    ) -> User:
        """Keep display_name/email in sync with the IdP on every login.

        Only active when ``auto_provision_login`` is on — then the IdP is
        the source of truth for these two fields so a rename upstream does
        not drift. No-op when the row already matches.
        """
        if not self._config.auto_provision_login:
            return user

        derived_display = self._display_name_from_claims(claims, sub)
        raw_email = claims.get("email")
        derived_email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None

        updates: dict[str, Any] = {}
        if derived_display and user.display_name != derived_display:
            updates["display_name"] = derived_display
        if derived_email is not None and user.email != derived_email:
            updates["email"] = derived_email
        if not updates:
            return user

        try:
            refreshed = await self._user_repo.update_user(user.id, **updates)
        except Exception as e:
            logger.warning(f"OIDC auto-provision sync failed for user_id={user.id}: {e}")
            return user
        return refreshed or user

    async def _apply_claim_mapping(self, user: User, bundle: Any) -> User:
        """Apply the optional ``OIDC_CLAIM_MAPPING`` (display_name/email only)."""
        mapping = self._parse_claim_mapping(self._config.claim_mapping)
        if not mapping:
            return user

        if self._config.claim_source == "userinfo":
            try:
                claims_for_mapping = await self._require_client().fetch_userinfo(bundle.access_token)
            except Exception as e:
                logger.warning(f"OIDC userinfo fetch failed: {e}")
                raise OIDCFlowError("Failed to fetch userinfo from IdP.") from e
            # OIDC Core 1.0 §5.3.2: the RP MUST verify that the sub in the
            # userinfo response matches the sub in the ID token before trusting
            # any claims.
            id_token_sub = bundle.claims.get("sub")
            userinfo_sub = claims_for_mapping.get("sub")
            if userinfo_sub != id_token_sub:
                logger.warning(
                    f"OIDC userinfo sub mismatch — "
                    f"id_token_sub={id_token_sub[:8] + '***' if isinstance(id_token_sub, str) and len(id_token_sub) > 8 else '***'}, "
                    f"userinfo_sub={userinfo_sub[:8] + '***' if isinstance(userinfo_sub, str) and len(userinfo_sub) > 8 else '***'}"
                )
                raise OIDCFlowError(
                    "Userinfo sub does not match ID token sub.",
                    status_code=401,
                )
        else:
            claims_for_mapping = bundle.claims

        updates: dict[str, Any] = {}
        for db_field, claim in mapping.items():
            value = claims_for_mapping.get(claim)
            if value is None:
                continue
            if getattr(user, db_field, None) == value:
                continue
            updates[db_field] = value
        if not updates:
            return user

        try:
            refreshed = await self._user_repo.update_user(user.id, **updates)
        except Exception as e:
            logger.warning(f"update_user failed for user_id={user.id}: {e}")
            return user
        return refreshed or user


__all__ = [
    "AuthService",
    "OIDCFlowError",
    "LoginRedirect",
    "CallbackResult",
    "SESSION_COOKIE_NAME",
]
