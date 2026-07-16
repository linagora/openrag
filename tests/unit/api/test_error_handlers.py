"""Tests for :mod:`api.error_handlers` (Phase 10B).

Exercises the FastAPI handler wiring end-to-end via ``TestClient`` so a
regression in the response shape (which the Robot Framework suite and
several router tests still assert byte-for-byte) is caught immediately
rather than at integration time.
"""

from __future__ import annotations

import pytest
from api.error_handlers import _STATUS_MAP, _status_for, register_error_handlers
from core.utils.exceptions import (
    ConflictError,
    DocumentNotFoundError,
    NotFoundError,
    OpenRAGError,
    PartitionNotFoundError,
    QuotaExceededError,
    ValidationError,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


@pytest.fixture()
def app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/openrag")
    async def _raise_openrag() -> None:
        raise OpenRAGError("boom", code="BOOM", status_code=418, foo="bar")

    @app.get("/raise/not_found")
    async def _raise_not_found() -> None:
        raise DocumentNotFoundError("missing doc")

    @app.get("/raise/quota")
    async def _raise_quota() -> None:
        raise QuotaExceededError("too many files")

    @app.get("/raise/validation")
    async def _raise_validation() -> None:
        raise ValidationError("bad input")

    @app.get("/raise/conflict")
    async def _raise_conflict() -> None:
        raise ConflictError("already in use")

    @app.get("/raise/unknown")
    async def _raise_unknown() -> None:
        raise RuntimeError("kaboom")

    @app.get("/raise/with-request-id")
    async def _raise_with_request_id(request: Request) -> None:
        # Simulates Phase 10C's RequestIdMiddleware having populated the
        # state before the route ran.
        request.state.request_id = "req_test_123"
        raise NotFoundError("resource gone")

    return app


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so the catch-all handler can actually
    # respond instead of TestClient re-raising the RuntimeError.
    return TestClient(app, raise_server_exceptions=False)


def test_openrag_error_uses_declared_status_code(client: TestClient) -> None:
    """OpenRAGError(status_code=418) -> the handler must honour 418."""
    response = client.get("/raise/openrag")
    assert response.status_code == 418
    body = response.json()
    # Legacy contract: top-level {"detail": "[CODE]: message", "extra": {...}}.
    assert body == {"detail": "[BOOM]: boom", "extra": {"foo": "bar"}}


def test_subclass_status_code_is_honored(client: TestClient) -> None:
    """A NotFoundError subclass must map to 404 from its own attribute."""
    response = client.get("/raise/not_found")
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "[DOCUMENT_NOT_FOUND]: missing doc"
    assert body["extra"] == {}


def test_quota_exceeded_maps_to_429(client: TestClient) -> None:
    response = client.get("/raise/quota")
    assert response.status_code == 429


def test_validation_error_maps_to_422(client: TestClient) -> None:
    response = client.get("/raise/validation")
    assert response.status_code == 422


def test_conflict_error_maps_to_409(client: TestClient) -> None:
    response = client.get("/raise/conflict")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "[CONFLICT]: already in use"


def test_unknown_exception_returns_500_with_legacy_body(client: TestClient) -> None:
    """A bare ``RuntimeError`` hits the catch-all and must produce the
    legacy ``[UNEXPECTED_ERROR]`` body — Robot Framework asserts on it."""
    response = client.get("/raise/unknown")
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "detail": "[UNEXPECTED_ERROR]: An unexpected error occurred",
        "extra": {},
    }


def test_request_id_is_injected_into_extra_when_set(client: TestClient) -> None:
    """When RequestIdMiddleware populates ``request.state.request_id`` the
    handler must surface it inside ``extra`` (additive — does not
    replace existing keys)."""
    response = client.get("/raise/with-request-id")
    assert response.status_code == 404
    body = response.json()
    assert body["extra"] == {"request_id": "req_test_123"}
    assert body["detail"] == "[NOT_FOUND]: resource gone"


# ---------------------------------------------------------------------------
# _status_for — unit-level checks (no FastAPI)
# ---------------------------------------------------------------------------


def test_status_for_prefers_explicit_attribute() -> None:
    """``exc.status_code`` wins over the MRO map — that is the path every
    current OpenRAGError subclass takes."""
    exc = PartitionNotFoundError("nope")
    assert exc.status_code == 404
    assert _status_for(exc) == 404


def test_status_for_falls_back_to_mro_walk() -> None:
    """When status_code is absent the MRO walk picks the tightest match
    in ``_STATUS_MAP`` — ``NotFoundError`` here, not ``OpenRAGError``."""

    class StatuslessNotFound(NotFoundError):
        def __init__(self) -> None:
            # Skip the parent __init__ so ``status_code`` is never set.
            Exception.__init__(self, "no status")
            self.message = "no status"
            self.code = "STATUSLESS"
            self.extra: dict[str, object] = {}

    exc = StatuslessNotFound()
    assert getattr(exc, "status_code", None) is None
    assert _status_for(exc) == 404


def test_status_for_defaults_to_500_for_arbitrary_exception() -> None:
    """A non-OpenRAGError exception has no status_code and no MRO match
    — the safety-net default is 500."""
    assert _status_for(RuntimeError("x")) == 500


def test_status_map_lists_specific_classes_first() -> None:
    """Sanity check: the dict ordering relied on by the MRO walk has the
    more specific classes before their bases, so ``AuthenticationError``
    resolves to 401 rather than ``AuthError``'s 403, etc."""
    keys = list(_STATUS_MAP)
    auth_specific = keys.index(
        __import__("core.utils.exceptions", fromlist=["AuthenticationError"]).AuthenticationError
    )
    auth_base = keys.index(__import__("core.utils.exceptions", fromlist=["AuthError"]).AuthError)
    assert auth_specific < auth_base
