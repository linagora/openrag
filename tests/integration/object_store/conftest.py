"""Fixtures for the S3/MinIO ObjectStore integration conformance run.

Auto-skips when the object store is unreachable (``OBJECT_STORE_TEST_ENDPOINT``
env, default local MinIO). Each test session uses a throwaway bucket so objects
don't bleed between runs.
"""

from __future__ import annotations

import os
import uuid

import pytest
from services.object_store.s3 import S3ObjectStore

ENDPOINT = os.environ.get("OBJECT_STORE_TEST_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.environ.get("OBJECT_STORE_TEST_ACCESS_KEY", "minioadmin")
SECRET_KEY = os.environ.get("OBJECT_STORE_TEST_SECRET_KEY", "minioadmin")


async def _reachable(store: S3ObjectStore) -> bool:
    try:
        await store._ensure_bucket()
        return True
    except Exception:
        return False


@pytest.fixture
async def object_store():
    store = S3ObjectStore(
        endpoint_url=ENDPOINT,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        bucket=f"openrag-test-{uuid.uuid4().hex[:8]}",
    )
    if not await _reachable(store):
        await store.aclose()
        pytest.skip(f"object store not reachable at {ENDPOINT}")
    try:
        yield store
    finally:
        await store.aclose()
