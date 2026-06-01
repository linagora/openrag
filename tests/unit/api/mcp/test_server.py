"""Tests for the MCP server entrypoint: auth context, middleware, tool wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.mcp import auth_context as ac
from api.mcp import server
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# auth_context
# ---------------------------------------------------------------------------


def test_auth_context_roundtrip():
    assert ac.get_user_id() is None
    assert ac.is_admin() is False
    assert ac.get_allowed_partitions() is None

    tokens = ac.set_auth_context(user_id=7, is_admin=True, allowed_partitions=["a"])
    assert ac.get_user_id() == 7
    assert ac.is_admin() is True
    assert ac.get_allowed_partitions() == ["a"]

    ac.reset_auth_context(tokens)
    assert ac.get_user_id() is None
    assert ac.is_admin() is False
    assert ac.get_allowed_partitions() is None


# ---------------------------------------------------------------------------
# Middleware fakes + helpers
# ---------------------------------------------------------------------------


class FakeAuthService:
    def __init__(self, *, user=None, by_token=None, partitions=None):
        self._user = user
        self._by_token = by_token
        self._partitions = partitions if partitions is not None else []

    async def get_user_for_request(self, user_id):
        return self._user

    async def get_user_by_token_for_request(self, token):
        return self._by_token

    async def list_user_partitions_for_request(self, user_id):
        return list(self._partitions)


def _install_container(monkeypatch, auth_service):
    monkeypatch.setattr(server, "_container", SimpleNamespace(auth_service=auth_service))


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": raw, "query_string": b""}
    return Request(scope)


async def _dispatch(monkeypatch, request):
    """Run the middleware and return (response, captured_context)."""
    captured: dict = {}

    async def call_next(_req):
        captured["user_id"] = ac.get_user_id()
        captured["is_admin"] = ac.is_admin()
        captured["allowed"] = ac.get_allowed_partitions()
        return Response("ok")

    mw = server.MCPAuthContextMiddleware(lambda scope, receive, send: None)
    response = await mw.dispatch(request, call_next)
    # context must be reset after dispatch regardless of outcome
    assert ac.get_user_id() is None
    assert ac.get_allowed_partitions() is None
    return response, captured


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_mode_resolves_user_one(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_MODE", raising=False)
    _install_container(
        monkeypatch,
        FakeAuthService(user={"id": 1, "is_admin": True}, partitions=[{"partition": "a"}, {"partition": "b"}]),
    )

    response, captured = await _dispatch(monkeypatch, _request())

    assert response.status_code == 200
    assert captured["user_id"] == 1
    # admin but SUPER_ADMIN_MODE off → explicit partition list, not wildcard
    assert captured["allowed"] == ["a", "b"]


@pytest.mark.asyncio
async def test_super_admin_gets_wildcard(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.setenv("SUPER_ADMIN_MODE", "true")
    _install_container(monkeypatch, FakeAuthService(user={"id": 1, "is_admin": True}, partitions=[{"partition": "a"}]))

    _, captured = await _dispatch(monkeypatch, _request())

    assert captured["is_admin"] is True
    assert captured["allowed"] == ["all"]


@pytest.mark.asyncio
async def test_token_mode_missing_token_403(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    _install_container(monkeypatch, FakeAuthService())

    response, captured = await _dispatch(monkeypatch, _request())

    assert response.status_code == 403
    assert captured == {}  # call_next never ran


@pytest.mark.asyncio
async def test_token_mode_invalid_token_403(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    _install_container(monkeypatch, FakeAuthService(by_token=None))

    response, _ = await _dispatch(monkeypatch, _request(headers={"authorization": "Bearer nope"}))

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_oidc_mode_no_token_is_not_dev_bypassed(monkeypatch):
    # H1 regression: AUTH_MODE=oidc with AUTH_TOKEN unset must NOT fall into the
    # user-1 admin dev bypass — a missing bearer is rejected.
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    _install_container(monkeypatch, FakeAuthService(user={"id": 1, "is_admin": True}))

    response, captured = await _dispatch(monkeypatch, _request())

    assert response.status_code == 403
    assert captured == {}  # never reached the tool as admin


@pytest.mark.asyncio
async def test_oidc_mode_valid_bearer_sets_context(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SUPER_ADMIN_MODE", raising=False)
    _install_container(
        monkeypatch,
        FakeAuthService(by_token={"id": 8, "is_admin": False}, partitions=[{"partition": "p"}]),
    )

    response, captured = await _dispatch(monkeypatch, _request(headers={"authorization": "Bearer good"}))

    assert response.status_code == 200
    assert captured["user_id"] == 8
    assert captured["allowed"] == ["p"]


@pytest.mark.asyncio
async def test_token_mode_valid_token_sets_context(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.delenv("SUPER_ADMIN_MODE", raising=False)
    _install_container(
        monkeypatch,
        FakeAuthService(by_token={"id": 42, "is_admin": False}, partitions=[{"partition": "team"}]),
    )

    response, captured = await _dispatch(monkeypatch, _request(headers={"authorization": "Bearer good"}))

    assert response.status_code == 200
    assert captured["user_id"] == 42
    assert captured["is_admin"] is False
    assert captured["allowed"] == ["team"]


# ---------------------------------------------------------------------------
# Tool → service wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tool_forwards_auth_context(monkeypatch):
    calls: dict = {}

    async def search_documents(**kwargs):
        calls.update(kwargs)
        return {"ok": True}

    fake_mcp = SimpleNamespace(search_documents=search_documents)
    monkeypatch.setattr(server, "_container", SimpleNamespace(mcp_service=fake_mcp))

    tokens = ac.set_auth_context(user_id=3, is_admin=False, allowed_partitions=["a"])
    try:
        out = await server.search_documents(query="hi", top_k=4)
    finally:
        ac.reset_auth_context(tokens)

    assert out == {"ok": True}
    assert calls["query"] == "hi"
    assert calls["top_k"] == 4
    assert calls["allowed_partitions"] == ["a"]


@pytest.mark.asyncio
async def test_delete_tool_forwards_user_id(monkeypatch):
    calls: dict = {}

    async def delete_file(**kwargs):
        calls.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(server, "_container", SimpleNamespace(mcp_service=SimpleNamespace(delete_file=delete_file)))

    tokens = ac.set_auth_context(user_id=9, is_admin=False, allowed_partitions=["p"])
    try:
        await server.delete_file(partition="p", file_id="f")
    finally:
        ac.reset_auth_context(tokens)

    assert calls["user_id"] == 9
    assert calls["partition"] == "p"
    assert calls["allowed_partitions"] == ["p"]


def test_require_container_raises_when_unset(monkeypatch):
    monkeypatch.setattr(server, "_container", None)
    with pytest.raises(RuntimeError):
        server._require_container()
