"""Unit tests for OIDC user/session methods on ``PartitionFileManager``.

These tests exercise the real ORM methods added in Phase 2 of the OIDC
integration (see ``.omc/plans/oidc-auth/plan.md`` §4, §6.3). They run
against an in-memory SQLite database — no Ray, no Postgres, no Milvus
required.

A ``PartitionFileManager`` instance is created without invoking
``__init__`` (which assumes Postgres + ``sqlalchemy_utils.database_exists``
semantics); we instead wire up a SQLite engine via ``Base.metadata.create_all``
and attach it to the object. This mirrors how ``PartitionFileManager``
itself initialises the schema in production (see ``utils.py``:``__init__``
which does the same ``Base.metadata.create_all`` call against Postgres).
"""

from datetime import datetime, timedelta

import pytest
from components.indexer.vectordb.models import Base, User
from components.indexer.vectordb.utils import PartitionFileManager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from utils.logger import get_logger


@pytest.fixture()
def pfm():
    """In-memory ``PartitionFileManager`` with a clean schema per test."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    # Bypass __init__ — it targets Postgres and uses sqlalchemy_utils'
    # create_database() which misbehaves with in-memory SQLite. Instead,
    # construct a bare PartitionFileManager and attach the schema-ready
    # engine ourselves. This keeps the method bodies under test untouched.
    mgr = PartitionFileManager.__new__(PartitionFileManager)
    mgr.engine = engine
    mgr.Session = Session
    mgr.logger = get_logger()
    mgr.file_quota_per_user = -1  # unlimited for tests

    yield mgr
    engine.dispose()


def _make_user(
    pfm,
    *,
    display_name: str = "Test User",
    email: str | None = None,
    external_user_id: str | None = None,
) -> int:
    """Helper — insert a user row directly (bypasses create_user's token
    hashing since we don't need the API token path here) and return the id."""
    with pfm.Session() as s:
        u = User(
            display_name=display_name,
            email=email,
            external_user_id=external_user_id,
            is_admin=False,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


# ---------------------------------------------------------------------------
# user lookup by external_user_id
# ---------------------------------------------------------------------------


def test_get_user_by_external_id_returns_user(pfm):
    user_id = _make_user(pfm, external_user_id="sub-abc-123")
    found = pfm.get_user_by_external_id("sub-abc-123")
    assert found is not None
    assert found["id"] == user_id
    assert found["external_user_id"] == "sub-abc-123"


def test_get_user_by_external_id_returns_none_for_unknown(pfm):
    _make_user(pfm, external_user_id="sub-abc-123")
    assert pfm.get_user_by_external_id("sub-other") is None


# ---------------------------------------------------------------------------
# update_user_fields — OIDC claim-mapping write path
# ---------------------------------------------------------------------------


def test_update_user_fields_updates_display_name_and_lowercases_email(pfm):
    user_id = _make_user(pfm, display_name="Old", email="old@example.com")
    pfm.update_user_fields(user_id, {"display_name": "New Name", "email": "NEW@Example.COM"})
    refreshed = pfm.get_user_by_external_id("sub-missing")  # None lookup, use session
    # Re-read via direct ORM since the user has no external_user_id set.
    with pfm.Session() as s:
        row = s.query(User).filter_by(id=user_id).first()
        assert row.display_name == "New Name"
        assert row.email == "new@example.com"  # normalised
    # `refreshed` is unrelated to the assertions — silences unused warning.
    assert refreshed is None


def test_update_user_fields_raises_on_non_whitelisted_field(pfm):
    user_id = _make_user(pfm)
    with pytest.raises(ValueError, match="non-whitelisted"):
        pfm.update_user_fields(user_id, {"is_admin": True})


def test_update_user_fields_raises_for_unknown_user(pfm):
    with pytest.raises(ValueError, match="not found"):
        pfm.update_user_fields(999999, {"display_name": "Ghost"})


def test_update_user_fields_drops_none_values(pfm):
    user_id = _make_user(pfm, display_name="Keep", email="keep@example.com")
    # None values are silently dropped — here every value is None so nothing
    # should be written and the row must remain intact.
    pfm.update_user_fields(user_id, {"display_name": None, "email": None})
    with pfm.Session() as s:
        row = s.query(User).filter_by(id=user_id).first()
        assert row.display_name == "Keep"
        assert row.email == "keep@example.com"


def test_update_user_fields_empty_dict_is_noop(pfm):
    """Empty dict must short-circuit before opening a DB session."""
    user_id = _make_user(pfm, display_name="Stable")
    # Monkey-patch Session to detect unexpected opens.
    original_session = pfm.Session
    opened = {"count": 0}

    class _SpySession:
        def __call__(self, *a, **kw):
            opened["count"] += 1
            return original_session(*a, **kw)

    pfm.Session = _SpySession()
    try:
        pfm.update_user_fields(user_id, {})
        assert opened["count"] == 0, "update_user_fields({}) must short-circuit before touching the DB"
    finally:
        pfm.Session = original_session


# ---------------------------------------------------------------------------
# create_oidc_session / get_oidc_session_by_token — round-trip
# ---------------------------------------------------------------------------


def _session_kwargs(user_id, *, sid="sid-xyz", session_token_plain="plain-token-aaaa"):
    now = datetime.now()
    return {
        "user_id": user_id,
        "sub": "sub-abc-123",
        "sid": sid,
        "session_token_plain": session_token_plain,
        "id_token_encrypted": b"\x01\x02\x03",
        "access_token_encrypted": b"\xaa\xbb\xcc",
        "refresh_token_encrypted": b"\xdd\xee\xff",
        "access_token_expires_at": now + timedelta(minutes=5),
        "session_expires_at": now + timedelta(hours=8),
    }


def test_create_and_get_oidc_session_round_trip(pfm):
    user_id = _make_user(pfm)
    kwargs = _session_kwargs(user_id, session_token_plain="tok-roundtrip-01")
    created = pfm.create_oidc_session(**kwargs)
    assert created["id"] is not None
    assert created["user_id"] == user_id
    assert created["sub"] == kwargs["sub"]
    assert created["sid"] == kwargs["sid"]
    # encrypted blobs passed through untouched
    assert created["access_token_encrypted"] == kwargs["access_token_encrypted"]
    assert created["revoked_at"] is None

    fetched = pfm.get_oidc_session_by_token("tok-roundtrip-01")
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["user_id"] == user_id


def test_get_oidc_session_by_token_returns_none_for_unknown(pfm):
    user_id = _make_user(pfm)
    pfm.create_oidc_session(**_session_kwargs(user_id, session_token_plain="tok-A"))
    assert pfm.get_oidc_session_by_token("tok-does-not-exist") is None


# ---------------------------------------------------------------------------
# revocation + expiry visibility
# ---------------------------------------------------------------------------


def test_get_oidc_session_returns_none_when_revoked(pfm):
    user_id = _make_user(pfm)
    created = pfm.create_oidc_session(**_session_kwargs(user_id, session_token_plain="tok-revoke"))
    pfm.revoke_oidc_session_by_id(created["id"])
    assert pfm.get_oidc_session_by_token("tok-revoke") is None


def test_get_oidc_session_returns_none_when_session_expired(pfm):
    user_id = _make_user(pfm)
    past = datetime.now() - timedelta(hours=1)
    pfm.create_oidc_session(
        user_id=user_id,
        sub="sub-abc-123",
        sid="sid-expired",
        session_token_plain="tok-expired",
        id_token_encrypted=None,
        access_token_encrypted=None,
        refresh_token_encrypted=None,
        access_token_expires_at=past,
        session_expires_at=past,  # already expired at insert time
    )
    assert pfm.get_oidc_session_by_token("tok-expired") is None


def test_revoke_oidc_sessions_by_sid_revokes_all_matching(pfm):
    user_id = _make_user(pfm)
    # Two sessions sharing one sid, one with a different sid.
    pfm.create_oidc_session(**_session_kwargs(user_id, sid="sid-shared", session_token_plain="tok-1"))
    pfm.create_oidc_session(**_session_kwargs(user_id, sid="sid-shared", session_token_plain="tok-2"))
    pfm.create_oidc_session(**_session_kwargs(user_id, sid="sid-other", session_token_plain="tok-3"))

    count = pfm.revoke_oidc_sessions_by_sid("sid-shared")
    assert count == 2

    # The revoked sessions must no longer be retrievable by token.
    assert pfm.get_oidc_session_by_token("tok-1") is None
    assert pfm.get_oidc_session_by_token("tok-2") is None
    # The other-sid session must still be live.
    assert pfm.get_oidc_session_by_token("tok-3") is not None


def test_revoke_oidc_sessions_by_sid_idempotent(pfm):
    """Calling twice with the same sid must only revoke non-revoked rows."""
    user_id = _make_user(pfm)
    pfm.create_oidc_session(**_session_kwargs(user_id, sid="sid-X", session_token_plain="tok-X"))
    first = pfm.revoke_oidc_sessions_by_sid("sid-X")
    second = pfm.revoke_oidc_sessions_by_sid("sid-X")
    assert first == 1
    assert second == 0  # already revoked


# ---------------------------------------------------------------------------
# update_oidc_session_tokens — post-refresh update
# ---------------------------------------------------------------------------


def test_update_oidc_session_tokens_updates_fields_and_last_refresh(pfm):
    user_id = _make_user(pfm)
    created = pfm.create_oidc_session(**_session_kwargs(user_id, session_token_plain="tok-refresh"))
    original_session_expiry = created["session_expires_at"]
    new_expiry = datetime.now() + timedelta(minutes=10)

    pfm.update_oidc_session_tokens(
        session_id=created["id"],
        access_token_encrypted=b"\x11\x22\x33",
        refresh_token_encrypted=b"\x44\x55\x66",
        access_token_expires_at=new_expiry,
    )

    fetched = pfm.get_oidc_session_by_token("tok-refresh")
    assert fetched is not None
    assert fetched["access_token_encrypted"] == b"\x11\x22\x33"
    assert fetched["refresh_token_encrypted"] == b"\x44\x55\x66"
    # datetime comparison — tolerate microsecond differences from DB round-trip
    assert abs((fetched["access_token_expires_at"] - new_expiry).total_seconds()) < 1
    assert fetched["last_refresh_at"] is not None
    # session_expires_at (the hard cap) is untouched
    assert fetched["session_expires_at"] == original_session_expiry


def test_update_oidc_session_tokens_accepts_none_refresh(pfm):
    """Some IdPs don't rotate refresh_token on refresh (omit refresh_token
    in the response). We must keep the old encrypted value."""
    user_id = _make_user(pfm)
    created = pfm.create_oidc_session(**_session_kwargs(user_id, session_token_plain="tok-nrr"))
    new_expiry = datetime.now() + timedelta(minutes=10)
    pfm.update_oidc_session_tokens(
        session_id=created["id"],
        access_token_encrypted=b"\x99\x88\x77",
        refresh_token_encrypted=None,
        access_token_expires_at=new_expiry,
    )
    fetched = pfm.get_oidc_session_by_token("tok-nrr")
    assert fetched["refresh_token_encrypted"] == b"\xdd\xee\xff"  # original


def test_update_oidc_session_tokens_raises_for_unknown_id(pfm):
    with pytest.raises(ValueError, match="does not exist"):
        pfm.update_oidc_session_tokens(
            session_id=424242,
            access_token_encrypted=b"x",
            refresh_token_encrypted=None,
            access_token_expires_at=datetime.now(),
        )


# ---------------------------------------------------------------------------
# cleanup_expired_oidc_sessions
# ---------------------------------------------------------------------------


def test_cleanup_deletes_only_rows_older_than_retention(pfm):
    """Rows are purged only once ``session_expires_at`` is older than
    the 7-day retention window. Still-live and recently-expired rows stay."""
    user_id = _make_user(pfm)
    now = datetime.now()

    # (1) Live — must stay
    pfm.create_oidc_session(
        user_id=user_id,
        sub="sub",
        sid="sid-live",
        session_token_plain="tok-live",
        id_token_encrypted=None,
        access_token_encrypted=None,
        refresh_token_encrypted=None,
        access_token_expires_at=now + timedelta(minutes=5),
        session_expires_at=now + timedelta(hours=1),
    )

    # (2) Recently expired (within 7-day retention) — must stay
    pfm.create_oidc_session(
        user_id=user_id,
        sub="sub",
        sid="sid-recent",
        session_token_plain="tok-recent",
        id_token_encrypted=None,
        access_token_encrypted=None,
        refresh_token_encrypted=None,
        access_token_expires_at=now - timedelta(hours=2),
        session_expires_at=now - timedelta(days=1),
    )

    # (3) Past retention — must be deleted
    pfm.create_oidc_session(
        user_id=user_id,
        sub="sub",
        sid="sid-stale",
        session_token_plain="tok-stale",
        id_token_encrypted=None,
        access_token_encrypted=None,
        refresh_token_encrypted=None,
        access_token_expires_at=now - timedelta(days=30),
        session_expires_at=now - timedelta(days=10),
    )

    deleted = pfm.cleanup_expired_oidc_sessions()
    assert deleted == 1

    # Live session still retrievable.
    assert pfm.get_oidc_session_by_token("tok-live") is not None
    # Recently expired: row kept, but still_masked as expired by get_by_token.
    assert pfm.get_oidc_session_by_token("tok-recent") is None
    # Stale: row gone entirely.
    assert pfm.get_oidc_session_by_token("tok-stale") is None
