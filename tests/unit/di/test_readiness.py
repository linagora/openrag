import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from core.config.root import Settings
from core.models.readiness import ModelEndpointDiscovery
from di.readiness import _check_ray, create_readiness_service
from services.orchestrators.readiness_service import ReadinessService


@pytest.mark.parametrize("reranker_enabled", [False, True])
async def test_wiring_checks_core_dependencies_and_database_endpoint_discovery(monkeypatch, reranker_enabled):
    settings = Settings(rdb={"password": "test"}, reranker={"provider": "infinity", "enabled": reranker_enabled})
    postgres = AsyncMock(side_effect=RuntimeError("database down"))
    milvus = AsyncMock(side_effect=RuntimeError("Milvus down"))
    discovery = AsyncMock(return_value=ModelEndpointDiscovery())
    publisher = MagicMock()
    monkeypatch.setattr("di.readiness._check_ray", AsyncMock(side_effect=RuntimeError("Ray down")))
    monkeypatch.setattr(
        "di.readiness.MODEL_ENDPOINT_READINESS_METRICS",
        SimpleNamespace(publish=publisher),
        raising=False,
    )
    container = SimpleNamespace(
        _require_settings=lambda: settings,
        catalog_store=SimpleNamespace(check_health=postgres),
        vector_store=SimpleNamespace(check_health=milvus),
        model_endpoint_repo=SimpleNamespace(discover_readiness_targets=discovery),
    )
    service = create_readiness_service(container)
    expected = {
        "postgres": "unavailable",
        "milvus": "unavailable",
        "ray": "unavailable",
        "embedder": "unresolvable",
        "llm": "unresolvable",
        "model_endpoint_discovery": "ok",
    }
    if reranker_enabled:
        expected["reranker"] = "unresolvable"
    assert await service.check() == expected
    default_model_kinds = ("embedder", "llm", "vlm", "reranker") if reranker_enabled else ("embedder", "llm", "vlm")
    discovery.assert_awaited_once_with(default_model_kinds=default_model_kinds)
    publisher.assert_called_once()


async def test_wiring_includes_stt_default_only_for_remote_audio_loader(monkeypatch):
    settings = Settings(
        rdb={"password": "test"},
        reranker={"provider": "infinity", "enabled": False},
        loader={"file_loaders": {"wav": "OpenAIAudioLoader"}},
    )
    discovery = AsyncMock(return_value=ModelEndpointDiscovery())
    monkeypatch.setattr("di.readiness._check_ray", AsyncMock())
    container = SimpleNamespace(
        _require_settings=lambda: settings,
        catalog_store=SimpleNamespace(check_health=AsyncMock()),
        vector_store=SimpleNamespace(check_health=AsyncMock()),
        model_endpoint_repo=SimpleNamespace(discover_readiness_targets=discovery),
    )

    await create_readiness_service(container).snapshot()

    discovery.assert_awaited_once_with(default_model_kinds=("embedder", "llm", "vlm", "stt"))


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
