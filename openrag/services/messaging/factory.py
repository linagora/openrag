"""Build the :class:`TaskQueue` backend from config (A2).

The single place the queue-backend choice lives. Producers (``marker_client``)
and consumers (the parser worker) depend only on the ``TaskQueue`` port, so
switching NATS -> Redis -> Temporal is a one-line change here — provided the
new backend passes ``tests/support/task_queue_contract.py``.

Lives in the ``services`` layer (not ``di``) so the worker/dispatcher — both
``services`` — can build a queue without importing ``di`` (which would invert the
layer dependency). ``di.messaging`` re-exports this for composition-root callers.
"""

from __future__ import annotations

from core.config import Settings, load_config
from core.ports.task_queue import TaskQueue


def build_task_queue(config: Settings | None = None) -> TaskQueue:
    config = config or load_config()
    backend = config.messaging.backend

    if backend == "nats":
        from services.messaging.nats_jetstream import NatsJetStreamTaskQueue

        return NatsJetStreamTaskQueue(config.messaging.nats_url, namespace=config.messaging.namespace)

    if backend == "in_memory":
        from services.messaging.in_memory import InMemoryTaskQueue

        return InMemoryTaskQueue()

    if backend == "postgres":
        # PgTaskQueue is a fallback; it needs an async session factory wired from
        # services.persistence before it can be enabled here.
        raise NotImplementedError(
            "postgres TaskQueue backend needs an async session factory; wire it "
            "via services.persistence before selecting messaging.backend=postgres"
        )

    raise ValueError(f"unknown messaging.backend {backend!r} (expected: nats | in_memory | postgres)")
