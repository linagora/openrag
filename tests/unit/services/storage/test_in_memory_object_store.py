"""The in-memory fake of tests/unit/conftest.py passes the ObjectStore conformance suite."""

import pytest
from object_store_conformance import ObjectStoreConformance


class TestInMemoryObjectStore(ObjectStoreConformance):
    @pytest.fixture
    def store(self, memory_object_store):
        return memory_object_store
