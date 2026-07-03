"""NATS JetStream backend against the shared TaskQueue conformance contract.

Same 7 assertions the in-memory backend passes — proving the NATS adapter
honors the exact contract the marker-serve worker and marker_client depend on.
Runs only when NATS/JetStream is reachable (see conftest); otherwise skipped.
"""

from __future__ import annotations

import pytest
from support.task_queue_contract import TaskQueueContract

pytestmark = pytest.mark.integration


class TestNatsJetStreamTaskQueue(TaskQueueContract):
    """`task_queue` fixture provided by the local conftest (NATS-backed)."""
