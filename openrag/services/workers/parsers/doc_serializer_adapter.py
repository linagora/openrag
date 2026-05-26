"""FileSerializer adapter over the DocSerializer Ray actor.

Replaces ``services/storage/serializer_ray_shim.py`` (Phase 9E). The adapter
lives in the workers layer because it wraps a worker Ray actor; the storage
layer no longer references Ray directly.
"""

from __future__ import annotations

from core.indexing.serializer import FileSerializer

_FALLBACK_TASK_ID = "tools-extract"


class DocSerializerAdapter(FileSerializer):
    """Implements FileSerializer by delegating to the DocSerializer Ray actor."""

    async def serialize(self, path: str, metadata: dict) -> str:
        import ray
        from config import load_config
        from services.workers.ray_utils import call_ray_actor_with_timeout

        cfg = load_config()
        timeout = cfg.ray.indexer.serialize_timeout
        task_id = ray.get_runtime_context().get_task_id() or _FALLBACK_TASK_ID
        serializer = ray.get_actor("DocSerializer", namespace="openrag")
        doc = await call_ray_actor_with_timeout(
            future=serializer.serialize_document.remote(task_id, path, metadata=metadata or {}),
            timeout=timeout,
            task_description=f"Serialization task {task_id}",
        )
        return doc.page_content


def from_ray_namespace() -> DocSerializerAdapter:
    """Build the adapter. Convenience for the composition root."""
    return DocSerializerAdapter()
