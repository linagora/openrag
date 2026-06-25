"""Unit tests for :class:`AuthService` (Phase 8A.1).

The OIDC primitives (PKCE/state generation, state-cookie signing, Fernet
token encryption, opaque session-token issuance) are exercised for real;
only the IdP-facing :class:`OIDCClient` and the persistence repos are
faked. Each test asserts behaviour the legacy router used to own.
"""

from __future__ import annotations

import pytest
from core.config.auth import OIDCConfig
from core.models.user import PartitionRole, User, UserPartition
from core.utils.exceptions import OpenRAGError
from cryptography.fernet import Fernet
from services.auth import StateCookieSerializer, hash_session_token
from services.auth.oidc_client import LogoutTokenClaims, TokenBundle
from services.orchestrators.auth_service import AuthService, OIDCFlowError

KEY = Fernet.generate_key().decode()


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeUserRepo:
    def __init__(self, users: dict[str, User] | None = None):
        self._by_ext = users or {}
        self._by_email = {
            user.email.strip().lower(): user
            for user in self._by_ext.values()
            if isinstance(user.email, str) and user.email.strip()
        }
        self.created: list[User] = []
        self.updated: list[tuple[int, dict]] = []
        self._next_id = 100

    async def get_user_by_external_id(self, external_id: str) -> User | None:
        return self._by_ext.get(external_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return self._by_email.get(email.strip().lower())

    async def create_user(self, user: User) -> User:
        normalized_email = user.email.strip().lower() if user.email else None
        if normalized_email and normalized_email in self._by_email:
            raise ValueError("duplicate key value violates unique constraint")
        self._next_id += 1
        user.id = self._next_id
        user.email = normalized_email
        self.created.append(user)
        if user.external_user_id:
            self._by_ext[user.external_user_id] = user
        if user.email:
            self._by_email[user.email] = user
        return user

    async def update_user(self, user_id: int, **fields):
        self.updated.append((user_id, fields))
        for u in self._by_ext.values():
            if u.id == user_id:
                for k, v in fields.items():
                    if k == "email" and isinstance(v, str):
                        v = v.strip().lower()
                    setattr(u, k, v)
                if u.email:
                    self._by_email[u.email] = u
                return u
        return None


class FakeSessionRepo:
    def __init__(self):
        self.created = []
        self.revoked_ids: list[int] = []
        self.revoked_sids: list[str] = []
        self.revoked_users: list[int] = []
        self._by_hash = {}

    async def create_session(self, session):
        self.created.append(session)
        self._by_hash[session.session_token_hash] = session
        return session

    async def get_by_token_hash(self, token_hash: str):
        return self._by_hash.get(token_hash)

    async def revoke_session(self, session_id: int) -> bool:
        self.revoked_ids.append(session_id)
        return True

    async def revoke_by_sid(self, sid: str) -> int:
        self.revoked_sids.append(sid)
        return 3

    async def revoke_by_user(self, user_id: int) -> int:
        self.revoked_users.append(user_id)
        return 2


class FakeOIDCClient:
    def __init__(self, *, bundle=None, logout_claims=None, meta=None, userinfo=None):
        self._bundle = bundle
        self._logout_claims = logout_claims
        self._meta = meta or {}
        self._userinfo = userinfo or {}
        self.exchange_calls: list[dict] = []

    async def build_authorization_url(self, *, state, nonce, code_challenge):
        return f"https://idp.example/auth?state={state}&cc={code_challenge}"

    async def exchange_code(self, *, code, code_verifier, expected_nonce):
        self.exchange_calls.append({"code": code, "cv": code_verifier, "nonce": expected_nonce})
        if isinstance(self._bundle, Exception):
            raise self._bundle
        return self._bundle

    async def verify_logout_token(self, token):
        if isinstance(self._logout_claims, Exception):
            raise self._logout_claims
        return self._logout_claims

    async def discover(self):
        return self._meta

    async def fetch_userinfo(self, access_token):
        return self._userinfo


def _cfg(**over) -> OIDCConfig:
    base = {
        "enabled": True,
        "client_id": "openrag",
        "token_encryption_key": KEY,
        "claim_source": "id_token",
        "claim_mapping": "",
        "post_logout_redirect_uri": "",
        "auto_provision_login": False,
    }
    base.update(over)
    return OIDCConfig(**base)


class FakeMembershipRepo:
    """In-memory ``partition_memberships`` keyed by (user_id, partition).

    ``fk_missing`` names partitions that do not exist in ``partitions`` — an
    ``assign_partition`` against one raises, mimicking the real FK violation so
    the sync's per-membership isolation can be exercised.
    """

    def __init__(self, *, seed=None, fk_missing=None):
        # seed: list of (user_id, partition, role-str)
        self._rows: dict[tuple[int, str], str] = {(uid, part): role for uid, part, role in (seed or [])}
        self._fk_missing = set(fk_missing or [])
        self.assigned: list[tuple[int, str, str]] = []
        self.updated: list[tuple[int, str, str]] = []
        self.removed: list[tuple[int, str]] = []

    async def list_user_partitions(self, user_id: int):
        return [
            UserPartition(user_id=uid, partition=part, role=PartitionRole(role))
            for (uid, part), role in self._rows.items()
            if uid == user_id
        ]

    async def assign_partition(self, assignment: UserPartition):
        if assignment.partition in self._fk_missing:
            raise RuntimeError("insert or update violates foreign key constraint")
        self._rows[(assignment.user_id, assignment.partition)] = assignment.role.value
        self.assigned.append((assignment.user_id, assignment.partition, assignment.role.value))
        return assignment

    async def update_partition_role(self, user_id: int, partition: str, role: PartitionRole) -> bool:
        self._rows[(user_id, partition)] = role.value
        self.updated.append((user_id, partition, role.value))
        return True

    async def remove_partition(self, user_id: int, partition: str) -> bool:
        existed = self._rows.pop((user_id, partition), None) is not None
        self.removed.append((user_id, partition))
        return existed


def _service(*, user_repo=None, session_repo=None, client=None, cfg=None, membership_repo=None) -> AuthService:
    return AuthService(
        user_repo=user_repo or FakeUserRepo(),
        oidc_session_repo=session_repo or FakeSessionRepo(),
        membership_repo=membership_repo if membership_repo is not None else object(),
        oidc_client=client,
        config=cfg or _cfg(),
    )


def _bundle(**over) -> TokenBundle:
    base = {
        "id_token": "idtok",
        "access_token": "acctok",
        "refresh_token": "reftok",
        "expires_in": 3600,
        "token_type": "Bearer",
        "claims": {"sub": "kc-alice", "sid": "sess-1"},
    }
    base.update(over)
    return TokenBundle(**base)


# --------------------------------------------------------------------------- #
# start_oidc_login
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_login_builds_url_and_roundtrips_state_cookie():
    svc = _service(client=FakeOIDCClient())
    result = await svc.start_oidc_login("/dashboard")

    assert result.authorization_url.startswith("https://idp.example/auth?state=")
    payload = StateCookieSerializer(secret_key=KEY).loads(result.state_cookie_value)
    assert payload.next_url == "/dashboard"
    # The state in the signed cookie must match the one in the auth URL.
    assert f"state={payload.state}" in result.authorization_url


@pytest.mark.asyncio
async def test_login_sanitizes_open_redirect():
    svc = _service(client=FakeOIDCClient())
    result = await svc.start_oidc_login("//evil.com/phish")
    payload = StateCookieSerializer(secret_key=KEY).loads(result.state_cookie_value)
    assert payload.next_url == "/"


# Regression for #360 — backslash-host and CRLF/NUL injection in next_url.
@pytest.mark.parametrize(
    "evil",
    [
        "/\\evil.com/phish",  # Chrome/Edge treat /\evil as netloc=evil.com
        "/\\\\evil.com",
        "/path\r\nLocation: http://evil",  # CRLF response splitting
        "/path\n\nSet-Cookie: stolen=1",
        "/path\x00admin",
        "//evil.com",
        "http://evil.com",
    ],
)
@pytest.mark.asyncio
async def test_login_blocks_backslash_and_crlf_in_next_url(evil):
    svc = _service(client=FakeOIDCClient())
    result = await svc.start_oidc_login(evil)
    payload = StateCookieSerializer(secret_key=KEY).loads(result.state_cookie_value)
    assert payload.next_url == "/", f"unsafe next_url leaked through: {payload.next_url!r}"


@pytest.mark.asyncio
async def test_login_preserves_safe_relative_path():
    svc = _service(client=FakeOIDCClient())
    result = await svc.start_oidc_login("/dashboard?foo=bar")
    payload = StateCookieSerializer(secret_key=KEY).loads(result.state_cookie_value)
    assert payload.next_url == "/dashboard?foo=bar"


@pytest.mark.asyncio
async def test_login_without_client_raises():
    svc = _service(client=None)
    with pytest.raises(OIDCFlowError) as ei:
        await svc.start_oidc_login("/")
    assert ei.value.status_code == 400


# --------------------------------------------------------------------------- #
# handle_oidc_callback
# --------------------------------------------------------------------------- #


async def _login_and_get_state(svc) -> tuple[str, str]:
    """Run login, return (state, signed_cookie_value)."""
    result = await svc.start_oidc_login("/home")
    payload = StateCookieSerializer(secret_key=KEY).loads(result.state_cookie_value)
    return payload.state, result.state_cookie_value


@pytest.mark.asyncio
async def test_callback_happy_path_creates_session():
    user = User(id=7, display_name="Alice", external_user_id="kc-alice")
    urepo = FakeUserRepo({"kc-alice": user})
    srepo = FakeSessionRepo()
    client = FakeOIDCClient(bundle=_bundle())
    svc = _service(user_repo=urepo, session_repo=srepo, client=client)

    state, cookie = await _login_and_get_state(svc)
    result = await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)

    assert result.next_url == "/home"
    assert len(srepo.created) == 1
    sess = srepo.created[0]
    assert sess.user_id == 7
    assert sess.sub == "kc-alice"
    assert sess.sid == "sess-1"
    # Only the hash is persisted; the plaintext cookie must hash to it.
    assert sess.session_token_hash == hash_session_token(result.session_cookie_value)
    # IdP tokens are stored encrypted, not in the clear.
    assert sess.access_token_encrypted not in (None, b"acctok")


@pytest.mark.asyncio
async def test_callback_missing_code_or_state():
    svc = _service(client=FakeOIDCClient(bundle=_bundle()))
    with pytest.raises(OIDCFlowError, match="Missing 'code' or 'state'"):
        await svc.handle_oidc_callback(code=None, state="x", state_cookie_raw="y")


@pytest.mark.asyncio
async def test_callback_state_mismatch():
    svc = _service(client=FakeOIDCClient(bundle=_bundle()))
    _, cookie = await _login_and_get_state(svc)
    with pytest.raises(OIDCFlowError, match="state mismatch"):
        await svc.handle_oidc_callback(code="abc", state="not-the-state", state_cookie_raw=cookie)


@pytest.mark.asyncio
async def test_callback_unregistered_user_rejected():
    svc = _service(user_repo=FakeUserRepo({}), client=FakeOIDCClient(bundle=_bundle()))
    state, cookie = await _login_and_get_state(svc)
    with pytest.raises(OIDCFlowError) as ei:
        await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)
    assert ei.value.status_code == 403
    assert ei.value.message == "User not registered"


@pytest.mark.asyncio
async def test_callback_auto_provisions_when_enabled():
    urepo = FakeUserRepo({})
    svc = _service(
        user_repo=urepo,
        client=FakeOIDCClient(bundle=_bundle(claims={"sub": "kc-bob", "name": "Bob", "email": "bob@x.io"})),
        cfg=_cfg(auto_provision_login=True),
    )
    state, cookie = await _login_and_get_state(svc)
    result = await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)

    assert len(urepo.created) == 1
    created = urepo.created[0]
    assert created.external_user_id == "kc-bob"
    assert created.display_name == "Bob"
    assert created.is_admin is False
    assert result.next_url == "/home"


@pytest.mark.asyncio
async def test_callback_auto_provision_email_collision_returns_conflict():
    existing = User(id=7, display_name="Existing", external_user_id="kc-old", email="alice@example.com")
    urepo = FakeUserRepo({"kc-old": existing})
    svc = _service(
        user_repo=urepo,
        client=FakeOIDCClient(bundle=_bundle(claims={"sub": "kc-new", "email": "alice@example.com"})),
        cfg=_cfg(auto_provision_login=True),
    )

    state, cookie = await _login_and_get_state(svc)
    with pytest.raises(OIDCFlowError) as ei:
        await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)

    assert ei.value.status_code == 409
    assert "already exists" in ei.value.message
    assert not urepo.created


@pytest.mark.asyncio
async def test_callback_code_exchange_failure_is_masked():
    svc = _service(client=FakeOIDCClient(bundle=RuntimeError("idp 500")))
    state, cookie = await _login_and_get_state(svc)
    with pytest.raises(OIDCFlowError, match="OIDC code exchange failed"):
        await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)


@pytest.mark.asyncio
async def test_claim_mapping_from_userinfo_updates_user():
    user = User(id=9, display_name="Old", external_user_id="kc-c", email="old@x.io")
    urepo = FakeUserRepo({"kc-c": user})
    client = FakeOIDCClient(
        bundle=_bundle(claims={"sub": "kc-c", "sid": "s"}),
        # OIDC Core 1.0 §5.3.2: userinfo must echo the same sub as the
        # id_token before claim mapping is allowed.
        userinfo={"sub": "kc-c", "mail": "new@x.io"},
    )
    svc = _service(
        user_repo=urepo,
        client=client,
        cfg=_cfg(claim_source="userinfo", claim_mapping="email:mail"),
    )
    state, cookie = await _login_and_get_state(svc)
    await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)

    assert urepo.updated and urepo.updated[0][1] == {"email": "new@x.io"}


@pytest.mark.asyncio
async def test_claim_mapping_rejects_userinfo_sub_mismatch():
    user = User(id=9, display_name="Old", external_user_id="kc-c", email="old@x.io")
    urepo = FakeUserRepo({"kc-c": user})
    client = FakeOIDCClient(
        bundle=_bundle(claims={"sub": "kc-c", "sid": "s"}),
        # userinfo sub differs from id_token sub — token-substitution scenario.
        userinfo={"sub": "kc-attacker", "mail": "evil@x.io"},
    )
    svc = _service(
        user_repo=urepo,
        client=client,
        cfg=_cfg(claim_source="userinfo", claim_mapping="email:mail"),
    )
    state, cookie = await _login_and_get_state(svc)
    with pytest.raises(OIDCFlowError, match="Userinfo sub does not match"):
        await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)
    assert not urepo.updated


# --------------------------------------------------------------------------- #
# backchannel logout / logout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backchannel_logout_revokes_by_sid():
    srepo = FakeSessionRepo()
    claims = LogoutTokenClaims(iss="i", aud="openrag", sub="s", sid="sess-9", iat=0, jti=None)
    svc = _service(session_repo=srepo, client=FakeOIDCClient(logout_claims=claims))
    count = await svc.handle_backchannel_logout("tok")
    assert count == 3
    assert srepo.revoked_sids == ["sess-9"]


@pytest.mark.asyncio
async def test_backchannel_logout_invalid_token_carries_description():
    svc = _service(client=FakeOIDCClient(logout_claims=ValueError("bad aud")))
    with pytest.raises(OIDCFlowError) as ei:
        await svc.handle_backchannel_logout("tok")
    assert ei.value.error_description == "bad aud"


@pytest.mark.asyncio
async def test_backchannel_logout_sidless_is_noop_200():
    srepo = FakeSessionRepo()
    claims = LogoutTokenClaims(iss="i", aud="openrag", sub="s", sid=None, iat=0, jti=None)
    svc = _service(session_repo=srepo, client=FakeOIDCClient(logout_claims=claims))
    assert await svc.handle_backchannel_logout("tok") == 0
    assert srepo.revoked_sids == []


@pytest.mark.asyncio
async def test_backchannel_logout_rejects_replayed_jti():
    import time as _time

    from services.orchestrators import auth_service as _as

    _as._seen_logout_jti.clear()
    srepo = FakeSessionRepo()
    claims = LogoutTokenClaims(
        iss="i",
        aud="openrag",
        sub="s",
        sid="sess-9",
        iat=0,
        jti="jti-replay-1",
        exp=int(_time.time()) + 120,
    )
    svc = _service(session_repo=srepo, client=FakeOIDCClient(logout_claims=claims))
    # First processing succeeds and revokes the session(s).
    assert await svc.handle_backchannel_logout("tok") == 3
    # Re-processing the same jti is rejected as a replay.
    with pytest.raises(OIDCFlowError) as ei:
        await svc.handle_backchannel_logout("tok")
    assert ei.value.error_description == "logout_token replayed"


@pytest.mark.asyncio
async def test_revoke_oidc_sessions_by_user_delegates_to_repo():
    srepo = FakeSessionRepo()
    svc = _service(session_repo=srepo)

    assert await svc.revoke_oidc_sessions_by_user(42) == 2
    assert srepo.revoked_users == [42]


@pytest.mark.asyncio
async def test_logout_revokes_and_builds_end_session_url():
    # Seed a real session via the callback path so the stored id_token is
    # encrypted with the configured key.
    user = User(id=5, external_user_id="kc-d")
    urepo = FakeUserRepo({"kc-d": user})
    srepo = FakeSessionRepo()
    client = FakeOIDCClient(
        bundle=_bundle(claims={"sub": "kc-d", "sid": "z"}),
        meta={"end_session_endpoint": "https://idp.example/logout"},
    )
    svc = _service(user_repo=urepo, session_repo=srepo, client=client)
    state, cookie = await _login_and_get_state(svc)
    cb = await svc.handle_oidc_callback(code="abc", state=state, state_cookie_raw=cookie)

    target = await svc.logout(cb.session_cookie_value)
    assert target.startswith("https://idp.example/logout?")
    assert "client_id=openrag" in target
    assert "id_token_hint=idtok" in target
    assert srepo.revoked_ids == [srepo.created[0].id]


@pytest.mark.asyncio
async def test_logout_no_end_session_returns_none():
    svc = _service(client=FakeOIDCClient(meta={}))
    assert await svc.logout(None) is None


# --------------------------------------------------------------------------- #
# pure auth-policy helpers
# --------------------------------------------------------------------------- #


def test_require_admin():
    assert AuthService.require_admin({"is_admin": True}) == {"is_admin": True}
    with pytest.raises(OIDCFlowError.__bases__[0]):  # OpenRAGError subclass (AuthError)
        AuthService.require_admin({"is_admin": False})


def test_check_partition_access_role_hierarchy():
    parts = [{"partition": "p1", "role": "viewer"}]
    assert AuthService.check_partition_access(
        user={"is_admin": False}, partition="p1", user_partitions=parts, required_role="viewer"
    )
    with pytest.raises(Exception):
        AuthService.check_partition_access(
            user={"is_admin": False}, partition="p1", user_partitions=parts, required_role="owner"
        )


def test_check_partition_access_super_admin_bypass():
    assert AuthService.check_partition_access(
        user={"is_admin": True},
        partition="anything",
        user_partitions=[],
        required_role="owner",
        super_admin_mode=True,
    )


def test_validate_file_quota():
    # Admin bypass.
    AuthService.validate_file_quota({"is_admin": True}, pending_task_count=99, default_quota=1)
    # Disabled globally.
    AuthService.validate_file_quota({"file_count": 50}, pending_task_count=50, default_quota=-1)
    # Specific limit exceeded (3 indexed + 2 pending >= 5).
    with pytest.raises(OpenRAGError) as ei:
        AuthService.validate_file_quota({"file_count": 3, "file_quota": 5}, pending_task_count=2, default_quota=10)
    assert ei.value.code == "FILE_QUOTA_EXCEEDED"
    # Under the limit is fine.
    AuthService.validate_file_quota({"file_count": 1, "file_quota": 5}, pending_task_count=1, default_quota=10)
    # Regression: a per-user limit is enforced even when the global default
    # is unlimited (-1). The negative default used to short-circuit to
    # "unlimited" and ignore the per-user quota entirely.
    with pytest.raises(OpenRAGError) as ei:
        AuthService.validate_file_quota({"file_count": 10, "file_quota": 10}, pending_task_count=0, default_quota=-1)
    assert ei.value.code == "FILE_QUOTA_EXCEEDED"
    # ...but a user with no per-user quota still inherits the unlimited default.
    AuthService.validate_file_quota({"file_count": 999}, pending_task_count=0, default_quota=-1)


# --------------------------------------------------------------------------- #
# _sync_oidc_memberships (Phase 15)
# --------------------------------------------------------------------------- #


def _group_cfg(**over) -> OIDCConfig:
    return _cfg(
        claim_groups="groups",
        group_prefix="/openrag/",
        group_pattern=r"(.+)/(owner|editor|viewer)$",
        **over,
    )


@pytest.mark.asyncio
async def test_membership_sync_is_noop_when_disabled():
    repo = FakeMembershipRepo()
    svc = _service(cfg=_cfg(), membership_repo=repo)  # claim_groups unset
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/owner"]})
    assert repo.assigned == [] and repo.updated == [] and repo.removed == []


@pytest.mark.asyncio
async def test_membership_sync_adds_missing():
    repo = FakeMembershipRepo(seed=[(1, "alpha", "viewer")])
    svc = _service(cfg=_group_cfg(), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/viewer", "/openrag/beta/editor"]})
    # alpha unchanged (already viewer); beta added.
    assert repo.assigned == [(1, "beta", "editor")]
    assert repo.updated == []
    assert repo.removed == []


@pytest.mark.asyncio
async def test_membership_sync_updates_changed_role():
    repo = FakeMembershipRepo(seed=[(1, "alpha", "viewer")])
    svc = _service(cfg=_group_cfg(), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/owner"]})
    assert repo.updated == [(1, "alpha", "owner")]
    assert repo.assigned == []


@pytest.mark.asyncio
async def test_membership_sync_prune_off_keeps_stale():
    repo = FakeMembershipRepo(seed=[(1, "stale", "editor")])
    svc = _service(cfg=_group_cfg(group_sync_prune=False), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/viewer"]})
    assert repo.assigned == [(1, "alpha", "viewer")]
    assert repo.removed == []  # 'stale' kept


@pytest.mark.asyncio
async def test_membership_sync_prune_on_removes_stale():
    repo = FakeMembershipRepo(seed=[(1, "stale", "editor"), (1, "alpha", "viewer")])
    svc = _service(cfg=_group_cfg(group_sync_prune=True), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/viewer"]})
    assert repo.removed == [(1, "stale")]  # only the one absent from the token


@pytest.mark.asyncio
async def test_membership_sync_prune_skips_when_groups_claim_absent():
    """Mass-revocation guard: a missing groups claim (mapper disabled / wrong
    claim name) must NOT prune — that's 'no signal', not 'revoke everything'."""
    repo = FakeMembershipRepo(seed=[(1, "alpha", "editor"), (1, "beta", "viewer")])
    svc = _service(cfg=_group_cfg(group_sync_prune=True), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"sub": "kc-1"})  # no 'groups' key
    assert repo.removed == []  # nothing wiped


@pytest.mark.asyncio
async def test_membership_sync_prune_skips_when_groups_claim_is_null():
    """An explicit ``groups: null`` is also treated as no assertion."""
    repo = FakeMembershipRepo(seed=[(1, "alpha", "editor")])
    svc = _service(cfg=_group_cfg(group_sync_prune=True), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": None})
    assert repo.removed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_claim", [42, {"a": 1}, 3.14, True])
async def test_membership_sync_prune_skips_when_groups_claim_is_malformed_type(bad_claim):
    """A present-but-malformed claim (number/dict) maps to zero groups via
    ``_coerce_groups`` — the same misconfiguration the guard defends against,
    so it must NOT prune. Only ``str``/``list`` count as an asserted signal."""
    repo = FakeMembershipRepo(seed=[(1, "alpha", "editor"), (1, "beta", "viewer")])
    svc = _service(cfg=_group_cfg(group_sync_prune=True), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": bad_claim})
    assert repo.removed == []  # nothing wiped on a garbage claim


@pytest.mark.asyncio
async def test_membership_sync_prune_runs_on_explicit_empty_group_list():
    """``groups: []`` is a real signal (user genuinely in no groups) → prune."""
    repo = FakeMembershipRepo(seed=[(1, "alpha", "editor"), (1, "beta", "viewer")])
    svc = _service(cfg=_group_cfg(group_sync_prune=True), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": []})
    assert sorted(repo.removed) == [(1, "alpha"), (1, "beta")]


@pytest.mark.asyncio
async def test_membership_sync_unknown_partition_is_isolated():
    """A group naming a non-existent partition (FK violation) is logged and
    skipped; other valid memberships in the same token still apply."""
    repo = FakeMembershipRepo(fk_missing=["ghost"])
    svc = _service(cfg=_group_cfg(), membership_repo=repo)
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/ghost/owner", "/openrag/alpha/editor"]})
    # ghost failed, alpha succeeded — the login is never blocked.
    assert (1, "alpha", "editor") in repo.assigned
    assert (1, "ghost") not in [(u, p) for (u, p, _) in repo.assigned]


@pytest.mark.asyncio
async def test_membership_sync_survives_list_failure():
    class Boom(FakeMembershipRepo):
        async def list_user_partitions(self, user_id):
            raise RuntimeError("db down")

    repo = Boom()
    svc = _service(cfg=_group_cfg(), membership_repo=repo)
    # Must not raise.
    await svc._sync_oidc_memberships(User(id=1), {"groups": ["/openrag/alpha/owner"]})
    assert repo.assigned == []
