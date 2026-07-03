"""A2 — build_task_queue selects the backend from config (no connection made)."""

from __future__ import annotations

import pytest

from core.config.infrastructure import MessagingConfig
from core.config.root import Settings
from di.messaging import build_task_queue
from services.messaging.in_memory import InMemoryTaskQueue
from services.messaging.nats_jetstream import NatsJetStreamTaskQueue


def _cfg(backend: str) -> Settings:
    return Settings(messaging=MessagingConfig(backend=backend))


def test_build_in_memory():
    assert isinstance(build_task_queue(_cfg("in_memory")), InMemoryTaskQueue)


def test_build_nats_does_not_connect():
    # Constructor is lazy — building it must not require a running server.
    assert isinstance(build_task_queue(_cfg("nats")), NatsJetStreamTaskQueue)


def test_postgres_not_wired_yet():
    with pytest.raises(NotImplementedError):
        build_task_queue(_cfg("postgres"))


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        build_task_queue(_cfg("bogus"))
