"""Wire readiness to the same stores and model configuration as API requests."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import TYPE_CHECKING, cast

import ray
from services.orchestrators.readiness_service import ReadinessService, check_model_endpoint
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

    async def check_model(kind: str) -> None:
        # Resolve on every refresh so admin changes do not leave a stale target.
        config = getattr(settings.models, kind).get("default")
        if config is None:
            raise RuntimeError("Default model endpoint is not configured")
        await check_model_endpoint(config, model_type=kind)

    checks = {
        "postgres": cast(PostgresStore, container.catalog_store).check_health,
        "milvus": cast(MilvusVectorStore, container.vector_store).check_health,
        "ray": _check_ray,
        "embedder": partial(check_model, "embedder"),
        "llm": partial(check_model, "llm"),
    }
    if settings.reranker.enabled:
        checks["reranker"] = partial(check_model, "reranker")
    return ReadinessService(checks)
