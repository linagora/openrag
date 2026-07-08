from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from services.inference.healthcheck import (
    EndpointStatus,
    check_endpoint_health,
    check_infinity,
    check_model_available,
)


@pytest.fixture
def models_response():
    return httpx.Response(200, json={"data": [{"id": "mistral-small"}, {"id": "bge-m3"}]})


@pytest.fixture
def health_ok():
    return httpx.Response(200, json={"status": "ok"})


class TestCheckOpenAICompatible:
    @pytest.mark.asyncio
    async def test_healthy(self, models_response):
        transport = httpx.MockTransport(lambda req: models_response)
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_endpoint_health("http://vllm:8000")
        assert result.status == EndpointStatus.HEALTHY
        assert "mistral-small" in result.models
        assert "bge-m3" in result.models
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_strips_trailing_slash(self, models_response):
        transport = httpx.MockTransport(lambda req: models_response)
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_endpoint_health("http://vllm:8000/")
        assert result.url == "http://vllm:8000"

    @pytest.mark.asyncio
    async def test_unhealthy_status(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(503))
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_endpoint_health("http://vllm:8000")
        assert result.status == EndpointStatus.UNHEALTHY
        assert result.http_status == 503

    @pytest.mark.asyncio
    async def test_connection_error(self):
        async def raise_connect_error(*a, **kw):
            raise httpx.ConnectError("Connection refused")

        client = AsyncMock()
        client.get = raise_connect_error
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("services.inference.healthcheck.httpx.AsyncClient", return_value=client):
            result = await check_endpoint_health("http://vllm:8000")
        assert result.status == EndpointStatus.UNREACHABLE
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_timeout(self):
        async def raise_timeout(*a, **kw):
            raise httpx.TimeoutException("timed out")

        client = AsyncMock()
        client.get = raise_timeout
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("services.inference.healthcheck.httpx.AsyncClient", return_value=client):
            result = await check_endpoint_health("http://vllm:8000")
        assert result.status == EndpointStatus.UNREACHABLE
        assert "timed out" in result.error


class TestCheckInfinity:
    @pytest.mark.asyncio
    async def test_healthy(self, health_ok):
        transport = httpx.MockTransport(lambda req: health_ok)
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_infinity("http://reranker:7997")
        assert result.status == EndpointStatus.HEALTHY
        assert result.latency_ms > 0

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(500))
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_infinity("http://reranker:7997")
        assert result.status == EndpointStatus.UNHEALTHY
        assert result.http_status == 500


class TestCheckModelAvailable:
    @pytest.mark.asyncio
    async def test_model_found(self, models_response):
        transport = httpx.MockTransport(lambda req: models_response)
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_model_available("http://vllm:8000", "mistral-small")
        assert result.status == EndpointStatus.HEALTHY

    @pytest.mark.asyncio
    async def test_model_not_found(self, models_response):
        transport = httpx.MockTransport(lambda req: models_response)
        with patch(
            "services.inference.healthcheck.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)
        ):
            result = await check_model_available("http://vllm:8000", "nonexistent-model")
        assert result.status == EndpointStatus.UNHEALTHY
        assert "nonexistent-model" in result.error
        assert "mistral-small" in result.error

    @pytest.mark.asyncio
    async def test_endpoint_unreachable_skips_model_check(self):
        async def raise_connect_error(*a, **kw):
            raise httpx.ConnectError("refused")

        client = AsyncMock()
        client.get = raise_connect_error
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("services.inference.healthcheck.httpx.AsyncClient", return_value=client):
            result = await check_model_available("http://vllm:8000", "mistral-small")
        assert result.status == EndpointStatus.UNREACHABLE
