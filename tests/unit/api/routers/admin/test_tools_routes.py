"""The tools router declares its own authenticated-user dependency."""

from __future__ import annotations

import pytest
from api.routers.admin import tools
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware


def _tools_app(user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(tools.router, prefix="/v1")
    if user is not None:

        class _AuthenticatedAs(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.user = user
                return await call_next(request)

        app.add_middleware(_AuthenticatedAs)
    return app


@pytest.mark.asyncio
async def test_tools_routes_refuse_a_request_with_no_authenticated_user(async_client_factory):
    async with async_client_factory(_tools_app()) as client:
        listed = await client.get("/v1/tools")
        executed = await client.post(
            "/v1/tools/execute",
            files={"file": ("note.txt", b"hello")},
            data={"tool": '{"name": "extractText"}'},
        )

    assert listed.status_code == 401
    assert executed.status_code == 401


@pytest.mark.asyncio
async def test_tools_list_is_served_to_an_authenticated_user(async_client_factory):
    async with async_client_factory(_tools_app(user={"id": 7, "is_admin": False})) as client:
        response = await client.get("/v1/tools")

    assert response.status_code == 200
    assert [tool["name"] for tool in response.json()] == ["extractText"]
