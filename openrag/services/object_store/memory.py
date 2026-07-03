"""In-memory :class:`ObjectStore` — the reference backend for tests and dev.

Holds objects in a dict. Passes the same ``object_store_contract`` as the S3
adapter, which is what makes the S3/MinIO backend provably interchangeable. Not
for production (no persistence, single-process).
"""

from __future__ import annotations

from core.ports.object_store import ObjectNotFound, ObjectStore


class InMemoryObjectStore(ObjectStore):
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        self._objects[key] = bytes(data)

    async def get(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError:
            raise ObjectNotFound(key)

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)


__all__ = ["InMemoryObjectStore"]
