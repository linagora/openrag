"""Object-store port — backend-agnostic blob handoff.

This is the seam that lets the app (producer) hand a large file to an off-Ray
parser worker (consumer) **without pushing the bytes through the message broker**.
The producer uploads the bytes under a key, puts only the *key* in the task
payload, and the worker fetches the bytes back by key. Both sides depend only on
this interface; the concrete backend (MinIO/S3 now, GCS/Azure later) is chosen in
the DI layer, so a swap never touches producers or consumers.

Why a dedicated port rather than reusing an existing repo: the vector/RDB stores
are *domain* persistence; this is *transient scratch* storage for inter-service
file transfer, with a different lifecycle (write-once, read-once, TTL-reaped).

Keys are opaque to the store. Callers pick content-addressed, unique keys (see
``MarkerServeClient``) so uploads never collide and cleanup is race-free.

The interface is async so a slow network round-trip never blocks the event loop;
sync SDKs (boto3) are wrapped with ``asyncio.to_thread`` in their adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ObjectStoreError(Exception):
    """Base class for object-store failures."""


class ObjectNotFound(ObjectStoreError):
    """Raised by :meth:`ObjectStore.get` when the key does not exist."""


class ObjectStore(ABC):
    """Backend-agnostic async blob store for inter-service file handoff."""

    @abstractmethod
    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        """Store ``data`` under ``key``, overwriting any existing object.

        Idempotent for a fixed (key, data): re-uploading the same key is safe.
        """
        ...

    @abstractmethod
    async def get(self, key: str) -> bytes:
        """Return the bytes stored under ``key``. Raise :class:`ObjectNotFound`
        if the key is absent."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove ``key``. A no-op if the key is already gone (idempotent), so
        best-effort cleanup never has to guard against double-delete."""
        ...

    async def aclose(self) -> None:
        """Release backend resources (connections, sessions).

        Default no-op so simple adapters need not override; connection-holding
        adapters override to close cleanly.
        """
        return None


__all__ = ["ObjectStore", "ObjectStoreError", "ObjectNotFound"]
