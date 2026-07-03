"""In-memory backend against the shared TaskQueue conformance contract.

The contract itself lives in ``tests/support/task_queue_contract.py`` so every
backend (in-memory here, NATS in tests/integration, later Redis/Temporal) runs
the *same* assertions. Adding a backend = a ~5-line subclass with its fixture.
"""

from __future__ import annotations

import pytest
from support.task_queue_contract import TaskQueueContract

from services.messaging.in_memory import InMemoryTaskQueue


class TestInMemoryTaskQueue(TaskQueueContract):
    @pytest.fixture
    async def task_queue(self):
        queue = InMemoryTaskQueue()
        try:
            yield queue
        finally:
            await queue.aclose()
