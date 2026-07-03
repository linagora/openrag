"""Build the :class:`ObjectStore` backend from config.

The single place the blob-store choice lives. The producer (``MarkerServeClient``)
and the consumer (the parser worker) depend only on the ``ObjectStore`` port, so
switching MinIO -> AWS S3 -> GCS is a one-line change here — provided the new
backend passes ``tests/support/object_store_contract.py``.
"""

from __future__ import annotations

from core.config import Settings, load_config
from core.ports.object_store import ObjectStore


def build_object_store(config: Settings | None = None) -> ObjectStore:
    config = config or load_config()
    cfg = config.object_store
    backend = cfg.backend

    if backend == "s3":
        from services.object_store.s3 import S3ObjectStore

        return S3ObjectStore(
            endpoint_url=cfg.endpoint_url,
            access_key=cfg.access_key,
            secret_key=cfg.secret_key,
            bucket=cfg.bucket,
            region=cfg.region,
        )

    if backend == "in_memory":
        from services.object_store.memory import InMemoryObjectStore

        return InMemoryObjectStore()

    raise ValueError(f"unknown object_store.backend {backend!r} (expected: s3 | in_memory)")
