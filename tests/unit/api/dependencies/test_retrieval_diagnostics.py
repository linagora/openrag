import pytest
from api.dependencies.retrieval_diagnostics import RetrievalDiagnosticsGuard
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_diagnostics_reject_non_admin_users():
    guard = RetrievalDiagnosticsGuard("2/minute")

    with pytest.raises(HTTPException) as error:
        await guard.authorize({"id": 7, "is_admin": False})

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_diagnostics_have_a_separate_admin_rate_limit():
    guard = RetrievalDiagnosticsGuard("2/minute")
    admin = {"id": 1, "is_admin": True}

    await guard.authorize(admin)
    await guard.authorize(admin)

    with pytest.raises(HTTPException) as error:
        await guard.authorize(admin)

    assert error.value.status_code == 429
    assert int(error.value.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_invalid_configured_rate_limit_falls_back_to_safe_default(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_DIAGNOSTICS_RATE_LIMIT", "not-a-rate-limit")
    guard = RetrievalDiagnosticsGuard()
    admin = {"id": 1, "is_admin": True}

    for _ in range(120):
        await guard.authorize(admin)

    with pytest.raises(HTTPException) as error:
        await guard.authorize(admin)

    assert error.value.status_code == 429
