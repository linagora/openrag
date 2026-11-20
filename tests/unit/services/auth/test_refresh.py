from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from services.auth.refresh import refresh_session_if_needed


@pytest.fixture
def refresh_clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.current

    FrozenDatetime.current = FrozenDatetime(2026, 1, 1)
    monkeypatch.setattr("services.auth.refresh.datetime", FrozenDatetime)
    return FrozenDatetime


@pytest.mark.asyncio
async def test_recently_refreshed_unexpired_session_is_reused_inside_refresh_buffer(monkeypatch, refresh_clock) -> None:
    now = refresh_clock.now()
    last_refresh_at = now - timedelta(seconds=1)
    session = {
        "id": 7,
        "refresh_token_encrypted": b"old-refresh-token",
        "last_refresh_at": last_refresh_at,
        "access_token_expires_at": now + timedelta(seconds=30),
    }
    refreshed_session = {
        **session,
        "last_refresh_at": now,
        "access_token_expires_at": now + timedelta(seconds=60),
    }
    auth_service = type("AuthService", (), {})()
    auth_service.get_oidc_session_by_id_for_request = AsyncMock(return_value=refreshed_session)
    get_oidc_client = Mock()
    monkeypatch.setattr("services.auth.refresh.get_oidc_client", get_oidc_client)

    result = await refresh_session_if_needed(session=session, enc_key="test-key", auth_service=auth_service)

    assert result == refreshed_session
    auth_service.get_oidc_session_by_id_for_request.assert_awaited_once_with(7)
    get_oidc_client.assert_not_called()


@pytest.mark.asyncio
async def test_recent_session_reread_is_rejected_if_token_expires_during_lookup(refresh_clock) -> None:
    now = refresh_clock.now()
    session = {
        "id": 8,
        "last_refresh_at": now,
        "access_token_expires_at": now + timedelta(seconds=1),
    }

    async def read_session(_session_id: int) -> dict:
        refresh_clock.current += timedelta(seconds=2)
        return session

    auth_service = type("AuthService", (), {})()
    auth_service.get_oidc_session_by_id_for_request = AsyncMock(side_effect=read_session)

    result = await refresh_session_if_needed(session=session, enc_key="test-key", auth_service=auth_service)

    assert result is None
    auth_service.get_oidc_session_by_id_for_request.assert_awaited_once_with(8)


@pytest.mark.asyncio
async def test_failed_refresh_reuses_newer_valid_session_inside_refresh_buffer(monkeypatch, refresh_clock) -> None:
    now = refresh_clock.now()
    session = {
        "id": 9,
        "refresh_token_encrypted": b"old-refresh-token",
        "last_refresh_at": now - timedelta(seconds=30),
        "access_token_expires_at": now + timedelta(seconds=10),
    }
    refreshed_session = {
        **session,
        "last_refresh_at": now,
        "access_token_expires_at": now + timedelta(seconds=30),
    }
    client = type("OIDCClient", (), {})()
    client.refresh_access_token = AsyncMock(side_effect=RuntimeError("refresh token was rotated"))
    get_oidc_client = Mock(return_value=client)
    monkeypatch.setattr("services.auth.refresh.decrypt_token", lambda *_args, **_kwargs: "old-refresh-token")
    monkeypatch.setattr("services.auth.refresh.get_oidc_client", get_oidc_client)
    auth_service = type("AuthService", (), {})()
    auth_service.get_oidc_session_by_id_for_request = AsyncMock(return_value=refreshed_session)

    result = await refresh_session_if_needed(session=session, enc_key="test-key", auth_service=auth_service)

    assert result == refreshed_session
    client.refresh_access_token.assert_awaited_once_with("old-refresh-token")
    auth_service.get_oidc_session_by_id_for_request.assert_awaited_once_with(9)
