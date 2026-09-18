"""Wire readiness to the same stores and model configuration as API requests."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import ray
from core.config.model_endpoints import ModelEndpointType
from core.observability.monitoring import MODEL_ENDPOINT_READINESS_METRICS
from services.orchestrators.readiness_service import ReadinessService
from services.storage.milvus_store import MilvusVectorStore
from services.storage.postgres_store import PostgresStore
from services.workers.ray_utils import call_ray_actor_method_with_timeout

_ray_actor = None
_ray_actor_lookup: Future | None = None
_ray_actor_lookup_lock = threading.Lock()
_ray_actor_lookup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readiness-ray-lookup")

if TYPE_CHECKING:
    from di.container import ServiceContainer


async def _lookup_ray_actor():
    global _ray_actor_lookup
    with _ray_actor_lookup_lock:
        lookup = _ray_actor_lookup
        if lookup is None:
            lookup = _ray_actor_lookup_executor.submit(ray.get_actor, "TaskStateManager", namespace="openrag")
            _ray_actor_lookup = lookup
    try:
        actor = await asyncio.shield(asyncio.wrap_future(lookup))
    except asyncio.CancelledError:
        raise
    except BaseException:
        with _ray_actor_lookup_lock:
            if _ray_actor_lookup is lookup:
                _ray_actor_lookup = None
        raise
    with _ray_actor_lookup_lock:
        if _ray_actor_lookup is lookup:
            _ray_actor_lookup = None
    return actor


async def _check_ray() -> None:
    global _ray_actor, _ray_actor_lookup
    if not ray.is_initialized():
        _ray_actor = None
        with _ray_actor_lookup_lock:
            _ray_actor_lookup = None
        raise RuntimeError("Ray is not initialized")
    if _ray_actor is None:
        _ray_actor = await _lookup_ray_actor()
    try:
        await call_ray_actor_method_with_timeout(
            _ray_actor.get_pool_info.remote, timeout=1.0, task_description="readiness"
        )
    except BaseException:
        _ray_actor = None
        raise


def create_readiness_service(container: ServiceContainer) -> ReadinessService:
    settings = container._require_settings()
    checks = {
        "postgres": cast(PostgresStore, container.catalog_store).check_health,
        "milvus": cast(MilvusVectorStore, container.vector_store).check_health,
        "ray": _check_ray,
    }
    summary_model_kinds: list[ModelEndpointType] = ["embedder", "llm"]
    default_model_kinds: list[ModelEndpointType] = ["embedder", "llm", "vlm"]
    if settings.reranker.enabled:
        summary_model_kinds.append("reranker")
        default_model_kinds.append("reranker")
    if "OpenAIAudioLoader" in settings.loader.file_loaders.model_dump().values():
        default_model_kinds.append("stt")

    async def discover_model_endpoints():
        return await container.model_endpoint_repo.discover_readiness_targets(
            default_model_kinds=tuple(default_model_kinds)
        )

    return ReadinessService(
        checks,
        discover_model_endpoints=discover_model_endpoints,
        summary_model_kinds=tuple(summary_model_kinds),
        publish=MODEL_ENDPOINT_READINESS_METRICS.publish,
    )
