"""NATS JetStream backend against the shared TaskQueue conformance contract.

Same 7 assertions the in-memory backend passes — proving the NATS adapter
honors the exact contract the marker-serve worker and marker_client depend on.
Runs only when NATS/JetStream is reachable (see conftest); otherwise skipped.
"""

from __future__ import annotations

import asyncio

import pytest
from services.messaging.nats_jetstream import _ACK_WAIT
from support.task_queue_contract import TaskQueueContract

pytestmark = pytest.mark.integration


class TestNatsJetStreamTaskQueue(TaskQueueContract):
    """`task_queue` fixture provided by the local conftest (NATS-backed)."""


async def test_consumer_uses_configured_ack_wait(task_queue):
    """Regression guard: the durable consumer must use our long ack_wait lease,
    not JetStream's 30s default — otherwise minutes-long marker parses get
    redelivered mid-flight (duplicate GPU work + false FAILED)."""

    async def handler(task):
        return {}

    task_queue.register("marker.parse", handler)
    run = asyncio.create_task(task_queue.run(concurrency=2))
    try:
        await (await task_queue.submit("marker.parse", {})).result(timeout=10)
        durable = f"{task_queue._ns}_worker_marker_parse"
        info = await task_queue._js.consumer_info(task_queue._stream, durable)
        assert info.config.ack_wait == _ACK_WAIT  # seconds
    finally:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
