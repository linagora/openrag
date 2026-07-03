"""Fixtures for the NATS TaskQueue integration conformance run.

Auto-skips when NATS/JetStream is unreachable (``NATS_TEST_URL`` env, default
local ``docker-compose.yaml`` here). Each test gets a fresh namespace so the
stream/KV don't bleed between cases.
"""

from __future__ import annotations

import os
import uuid

import nats
import pytest

from services.messaging.nats_jetstream import NatsJetStreamTaskQueue

NATS_URL = os.environ.get("NATS_TEST_URL", "nats://localhost:4222")


async def _jetstream_reachable() -> bool:
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=2)
    except Exception:
        return False
    try:
        await nc.jetstream().account_info()  # confirms JetStream is enabled
        return True
    except Exception:
        return False
    finally:
        await nc.close()


@pytest.fixture
async def task_queue():
    if not await _jetstream_reachable():
        pytest.skip(f"NATS/JetStream not reachable at {NATS_URL}")
    queue = NatsJetStreamTaskQueue(NATS_URL, namespace=f"t{uuid.uuid4().hex[:8]}")
    try:
        yield queue
    finally:
        await queue.aclose()
