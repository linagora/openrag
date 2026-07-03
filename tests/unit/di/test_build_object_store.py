"""build_object_store selects the backend from config (no connection made)."""

from __future__ import annotations

import pytest
from core.config.infrastructure import ObjectStoreConfig
from core.config.root import Settings
from di.object_store import build_object_store
from services.object_store.memory import InMemoryObjectStore
from services.object_store.s3 import S3ObjectStore


def _cfg(backend: str) -> Settings:
    return Settings(object_store=ObjectStoreConfig(backend=backend))


def test_build_in_memory():
    assert isinstance(build_object_store(_cfg("in_memory")), InMemoryObjectStore)


def test_build_s3_does_not_connect():
    # Constructor is lazy (bucket ensured on first use) — building it must not
    # require a running MinIO/S3.
    assert isinstance(build_object_store(_cfg("s3")), S3ObjectStore)


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        build_object_store(_cfg("bogus"))
