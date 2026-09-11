import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.config.root import Settings
from di.readiness import _check_ray, create_readiness_service
from services.orchestrators.readiness_service import ReadinessService


@pytest.mark.parametrize("reranker_enabled", [False, True])
async def test_wiring_checks_core_dependencies_and_current_default_models(monkeypatch, reranker_enabled):
    settings = Settings(rdb={"password": "test"}, reranker={"provider": "infinity", "enabled": reranker_enabled})
    config = ModelEndpointConfig(endpoint="https://model.test/v1", model_name="model")
    settings.models.llm["default"] = config
    settings.models.embedder["default"] = config
    postgres = AsyncMock(side_effect=RuntimeError("database down"))
    milvus = AsyncMock(side_effect=RuntimeError("Milvus down"))
    monkeypatch.setattr("di.readiness._check_ray", AsyncMock(side_effect=RuntimeError("Ray down")))
    model_probe = AsyncMock()
    monkeypatch.setattr("di.readiness.check_model_endpoint", model_probe)
    container = SimpleNamespace(
        _require_settings=lambda: settings,
        catalog_store=SimpleNamespace(check_health=postgres),
        vector_store=SimpleNamespace(check_health=milvus),
    )
    service = create_readiness_service(container)
    expected = {
        "postgres": "unavailable",
        "milvus": "unavailable",
        "ray": "unavailable",
        "embedder": "ok",
        "llm": "ok",
    }
    if reranker_enabled:
        expected["reranker"] = "unavailable"
    assert await service.check() == expected
    # A missing default must not silently report ready. Optional models are not checked.
    settings.models.llm.clear()
    assert (await create_readiness_service(container).check())["llm"] == "unavailable"


async def test_ray_probe_requires_a_successful_actor_call(monkeypatch):
    monkeypatch.setattr("di.readiness._ray_actor", None)
    monkeypatch.setattr("di.readiness.ray.is_initialized", lambda: True)
    method = AsyncMock(return_value={"pool_size": 1})
    actor = SimpleNamespace(get_pool_info=SimpleNamespace(remote=method))
    lookup = MagicMock(return_value=actor)
    monkeypatch.setattr("di.readiness.ray.get_actor", lookup)
    await _check_ray()
    lookup.assert_called_once_with("TaskStateManager", namespace="openrag")
    method.assert_awaited_once()


async def test_ray_probe_rejects_uninitialized_ray(monkeypatch):
    monkeypatch.setattr("di.readiness._ray_actor", None)
    monkeypatch.setattr("di.readiness.ray.is_initialized", lambda: False)
    with pytest.raises(RuntimeError, match="not initialized"):
        await _check_ray()


async def test_ray_probe_reuses_actor_handle_until_it_fails(monkeypatch):
    monkeypatch.setattr("di.readiness._ray_actor", None)
    monkeypatch.setattr("di.readiness.ray.is_initialized", lambda: True)
    method = AsyncMock(return_value={"pool_size": 1})
    actor = SimpleNamespace(get_pool_info=SimpleNamespace(remote=method))
    lookup = MagicMock(return_value=actor)
    monkeypatch.setattr("di.readiness.ray.get_actor", lookup)
    await _check_ray()
    await _check_ray()
    lookup.assert_called_once_with("TaskStateManager", namespace="openrag")
    assert method.await_count == 2


async def test_timed_out_ray_probe_reuses_the_in_flight_actor_lookup(monkeypatch):
    monkeypatch.setattr("di.readiness._ray_actor", None)
    monkeypatch.setattr("di.readiness.ray.is_initialized", lambda: True)
    release_lookup = threading.Event()
    lookup_started = threading.Event()
    method = AsyncMock(return_value={"pool_size": 1})
    actor = SimpleNamespace(get_pool_info=SimpleNamespace(remote=method))

    def blocked_lookup(*args, **kwargs):
        lookup_started.set()
        release_lookup.wait(timeout=1)
        return actor

    lookup = MagicMock(side_effect=blocked_lookup)
    monkeypatch.setattr("di.readiness.ray.get_actor", lookup)
    service = ReadinessService({"ray": _check_ray}, timeout=0.01, cache_ttl=0)

    try:
        assert await service.check() == {"ray": "timeout"}
        assert lookup_started.wait(timeout=1)
        assert await service.check() == {"ray": "timeout"}
        assert await service.check() == {"ray": "timeout"}
        assert lookup.call_count == 1
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "available"), timeout=0.1) == "available"
    finally:
        release_lookup.set()
