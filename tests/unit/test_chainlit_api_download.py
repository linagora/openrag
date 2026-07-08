"""Regression test for the standalone Chainlit app's source-download route.

In Ray Serve mode the API and Chainlit run on separate ports. Source previews
rewrite their file download links to the browser origin (the Chainlit host),
so the standalone Chainlit app (``chainlit_api.app``) must expose the
authorized ``/static/{extract_id}`` download route — otherwise PDFs/images/audio
from search sources would 404 against the Chainlit service.
"""

from __future__ import annotations

import importlib
import sys
import types

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


def _import_chainlit_api(monkeypatch):
    """Import ``chainlit_api`` with ``chainlit`` stubbed out."""
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
    return importlib.import_module("chainlit_api")


def test_standalone_chainlit_exposes_source_download_route(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "token")

    module = _import_chainlit_api(monkeypatch)

    routes = {getattr(r, "name", None): getattr(r, "path", None) for r in module.app.routes}
    assert routes.get("download_source") == "/static/{extract_id}"
