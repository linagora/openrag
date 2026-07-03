"""Reusable conformance contract for the :class:`ObjectStore` port.

Subclass :class:`ObjectStoreContract` and provide an async ``object_store``
fixture that yields a clean backend instance. Every backend adapter MUST pass
all of these — this is the seam that makes the in-memory -> MinIO/S3 -> GCS swap
provably safe.

The base class has no ``Test`` prefix, so pytest does not collect it directly;
only the concrete subclasses (in-memory unit test, MinIO integration test) run.
"""

from __future__ import annotations

import pytest
from core.ports.object_store import ObjectNotFound


class ObjectStoreContract:
    async def test_put_then_get_roundtrips(self, object_store):
        await object_store.put("k/1", b"hello world")
        assert await object_store.get("k/1") == b"hello world"

    async def test_get_missing_raises_object_not_found(self, object_store):
        with pytest.raises(ObjectNotFound):
            await object_store.get("nope/does-not-exist")

    async def test_put_overwrites(self, object_store):
        await object_store.put("k/2", b"first")
        await object_store.put("k/2", b"second")
        assert await object_store.get("k/2") == b"second"

    async def test_delete_removes(self, object_store):
        await object_store.put("k/3", b"data")
        await object_store.delete("k/3")
        with pytest.raises(ObjectNotFound):
            await object_store.get("k/3")

    async def test_delete_missing_is_noop(self, object_store):
        # Best-effort cleanup must never raise on an already-gone key.
        await object_store.delete("k/never-existed")

    async def test_binary_payload_is_byte_exact(self, object_store):
        blob = bytes(range(256)) * 8  # non-utf8, includes NULs
        await object_store.put("k/bin", blob)
        assert await object_store.get("k/bin") == blob

    async def test_keys_are_independent(self, object_store):
        await object_store.put("a", b"A")
        await object_store.put("b", b"B")
        assert await object_store.get("a") == b"A"
        assert await object_store.get("b") == b"B"
