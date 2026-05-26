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
