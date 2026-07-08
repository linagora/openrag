"""API unit-test fixtures: an ASGI HTTPX client factory for exercising
FastAPI apps in-process without a running server.

Most router/middleware unit tests build a small purpose-built ``FastAPI`` app
(mounting only the router under test) and override dependencies with mocks.
``async_client_factory`` wraps any such app in an ``httpx.AsyncClient`` backed
by ``ASGITransport``; ``async_client`` is a ready-to-use client over a bare app
for tests that supply their own routes via the returned app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI


@pytest.fixture()
def async_client_factory() -> Callable[[FastAPI], httpx.AsyncClient]:
    """Return a builder that wraps a FastAPI app in an ASGI-backed client.

    Apply ``app.dependency_overrides[...] = ...`` before issuing requests to
    swap real services for the mocks from ``tests/unit/conftest.py``.
    """

    def _build(app: FastAPI) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    return _build


@pytest_asyncio.fixture()
async def async_client(
    async_client_factory: Callable[[FastAPI], httpx.AsyncClient],
) -> AsyncIterator[httpx.AsyncClient]:
    """An ASGI client over a bare app; mount routes on ``client._transport.app``
    or prefer ``async_client_factory`` when you need a pre-built app."""
    app = FastAPI()
    client = async_client_factory(app)
    async with client:
        yield client
