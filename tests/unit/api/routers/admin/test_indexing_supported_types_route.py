"""The supported-types route declares its own authenticated-user dependency."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from api.routers.admin import indexing
from di.providers import get_config
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware


class _Section:
    def __init__(self, data: dict) -> None:
        self._data = data

    def model_dump(self) -> dict:
        return self._data

    def to_dict(self) -> dict:
        return self._data


def _indexing_app(user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(indexing.router, prefix="/indexer")
    cfg = SimpleNamespace(
        loader=SimpleNamespace(
            file_loaders=_Section({".pdf": "MarkerLoader"}),
            mimetypes=_Section({"application/pdf": ".pdf"}),
        )
    )
    app.dependency_overrides[get_config] = lambda: cfg
    if user is not None:

        class _AuthenticatedAs(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = user
                return await call_next(request)

        app.add_middleware(_AuthenticatedAs)
    return app


@pytest.mark.asyncio
async def test_supported_types_refuses_a_request_with_no_authenticated_user(async_client_factory):
    async with async_client_factory(_indexing_app()) as client:
        response = await client.get("/indexer/supported/types")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_supported_types_is_served_to_an_authenticated_user(async_client_factory):
    async with async_client_factory(_indexing_app(user={"id": 7, "is_admin": False})) as client:
        response = await client.get("/indexer/supported/types")

    assert response.status_code == 200
    assert response.json() == {"extensions": [".pdf"], "mimetypes": ["application/pdf"]}
