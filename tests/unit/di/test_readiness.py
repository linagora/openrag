from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config.model_endpoints import ModelEndpointConfig
from core.config.root import Settings
from di.readiness import _check_ray, create_readiness_service


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
