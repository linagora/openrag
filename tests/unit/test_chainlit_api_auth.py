from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient


def test_standalone_chainlit_redirects_unauthenticated_oidc_html(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "dummy")

    vectordb = MagicMock()
    vectordb.get_oidc_session_by_token = MagicMock()
    vectordb.get_oidc_session_by_token.remote = AsyncMock(return_value=None)

    dependencies = types.ModuleType("utils.dependencies")
    dependencies.get_vectordb = lambda: vectordb
    monkeypatch.setitem(sys.modules, "utils.dependencies", dependencies)

    chainlit = types.ModuleType("chainlit")
    chainlit_utils = types.ModuleType("chainlit.utils")

    def mount_chainlit(app: FastAPI, target: str, path: str):
        @app.get(f"{path}/")
        async def chainlit_page():
            return PlainTextResponse("chainlit")

    chainlit_utils.mount_chainlit = mount_chainlit
    chainlit.utils = chainlit_utils
    monkeypatch.setitem(sys.modules, "chainlit", chainlit)
    monkeypatch.setitem(sys.modules, "chainlit.utils", chainlit_utils)

    sys.modules.pop("chainlit_api", None)
    module = importlib.import_module("chainlit_api")

    with TestClient(module.app) as client:
        response = client.get("/chainlit/", headers={"accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login?next=%2Fchainlit%2F"


def _stub_chainlit(monkeypatch):
    chainlit = types.ModuleType("chainlit")
    chainlit_utils = types.ModuleType("chainlit.utils")

    def mount_chainlit(app: FastAPI, target: str, path: str):
        @app.get(f"{path}/")
        async def chainlit_page():
            return PlainTextResponse("chainlit")

    chainlit_utils.mount_chainlit = mount_chainlit
    chainlit.utils = chainlit_utils
    monkeypatch.setitem(sys.modules, "chainlit", chainlit)
    monkeypatch.setitem(sys.modules, "chainlit.utils", chainlit_utils)


def test_standalone_chainlit_oidc_cookie_uses_initialized_container(monkeypatch):
    """A session cookie must be looked up against an *initialized* container.

    Regression: in Ray Serve mode the standalone Chainlit app ran in its own
    process and never called ``ServiceContainer.initialize()``, so a logged-in
    user revisiting ``/chainlit/`` (cookie present) hit
    ``ConnectionManager.initialize() has not been called`` → 500. The lifespan
    now opens the container's pool; here we assert the cookie lookup reaches
    the auth service (rather than crashing) and redirects when the session is
    unknown.
    """
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_TOKEN_ENCRYPTION_KEY", "dummy")

    auth_service = MagicMock()
    auth_service.get_oidc_session_by_token_for_request = AsyncMock(return_value=None)

    container = MagicMock()
    container.auth_service = auth_service
    container.initialize = AsyncMock()
    container.shutdown = AsyncMock()

    _stub_chainlit(monkeypatch)
    sys.modules.pop("chainlit_api", None)
    module = importlib.import_module("chainlit_api")
    # The lifespan builds the container from this symbol — swap in a fake whose
    # pool is already "open" so no real Postgres/Milvus is required.
    monkeypatch.setattr(module, "ServiceContainer", lambda *a, **k: container)

    with TestClient(module.app) as client:
        client.cookies.set("openrag_session", "opaque-session-token")
        response = client.get(
            "/chainlit/",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

    container.initialize.assert_awaited_once()
    auth_service.get_oidc_session_by_token_for_request.assert_awaited_once_with("opaque-session-token")
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/login?next=%2Fchainlit%2F"
