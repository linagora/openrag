"""In-memory backend against the shared ObjectStore conformance contract.

The contract itself lives in ``tests/support/object_store_contract.py`` so every
backend (in-memory here, MinIO/S3 in tests/integration, later GCS/Azure) runs the
*same* assertions. Adding a backend = a ~5-line subclass with its fixture.
"""

from __future__ import annotations

import pytest
from services.object_store.memory import InMemoryObjectStore
from support.object_store_contract import ObjectStoreContract


class TestInMemoryObjectStore(ObjectStoreContract):
    @pytest.fixture
    async def object_store(self):
        store = InMemoryObjectStore()
        try:
            yield store
        finally:
            await store.aclose()
