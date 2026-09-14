import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from core.config.model_endpoints import ModelEndpointConfig
from services.orchestrators.readiness_service import ReadinessService, check_model_endpoint


async def test_checks_run_concurrently_and_share_cached_result():
    entered = 0
    all_entered = asyncio.Event()

    async def check():
        nonlocal entered
        entered += 1
        if entered == 2:
            all_entered.set()
        await all_entered.wait()

    service = ReadinessService({"postgres": check, "milvus": check})
    results = await asyncio.gather(*(service.check() for _ in range(3)))
    assert results == [{"postgres": "ok", "milvus": "ok"}] * 3
    assert entered == 2


async def test_outage_recovers_after_cache_expiry():
    check = AsyncMock(side_effect=[RuntimeError("secret connection string"), None])
    service = ReadinessService({"postgres": check}, cache_ttl=0)
    assert await service.check() == {"postgres": "unavailable"}
    assert await service.check() == {"postgres": "ok"}


async def test_hung_check_times_out_without_losing_healthy_results():
    async def hung():
        await asyncio.Event().wait()

    service = ReadinessService({"ray": hung, "postgres": AsyncMock()}, timeout=0.01)
    assert await service.check() == {"ray": "timeout", "postgres": "ok"}


async def test_cancellation_is_not_cached_as_an_outage():
    service = ReadinessService({"ray": AsyncMock(side_effect=asyncio.CancelledError)})
    with pytest.raises(asyncio.CancelledError):
        await service.check()


@pytest.mark.parametrize(
    "implementation,base,path",
    [
        ("vllm", "/v1/", "/v1/models"),
        ("ollama", "", "/v1/models"),
        ("ollama", "/v1", "/v1/models"),
        ("infinity", "", "/health"),
        ("tei", "", "/health"),
    ],
)
async def test_model_probe_uses_provider_path_and_credentials(respx_mock, implementation, base, path):
    probe = respx_mock.get("https://model.test" + path).respond(200, json={"data": [{"id": "model"}]})
    config = ModelEndpointConfig(
        endpoint="https://model.test" + base,
        model_name="model",
        extra={"implementation": implementation, "api_key": "test-key"},
    )
    await check_model_endpoint(config)
    assert probe.calls[0].request.headers["Authorization"] == "Bearer test-key"


async def test_reranker_defaults_to_infinity_health_probe(respx_mock):
    probe = respx_mock.get("https://model.test/health").respond(200)
    config = ModelEndpointConfig(endpoint="https://model.test", model_name="reranker")
    await check_model_endpoint(config, model_type="reranker")
    assert probe.called


@pytest.mark.parametrize("status,payload", [(401, {}), (503, {}), (200, {"data": []}), (200, {})])
async def test_model_probe_rejects_unavailable_or_missing_model(respx_mock, status, payload):
    respx_mock.get("https://model.test/v1/models").respond(status, json=payload)
    config = ModelEndpointConfig(endpoint="https://model.test/v1", model_name="model")
    service = ReadinessService({"llm": lambda: check_model_endpoint(config)})
    assert await service.check() == {"llm": "unavailable"}


async def test_httpx_timeout_is_reported_as_timeout(monkeypatch):
    async def timed_out():
        raise httpx.ReadTimeout("model timed out")

    service = ReadinessService({"llm": timed_out})
    assert await service.check() == {"llm": "timeout"}
