"""Unit tests for :class:`AdvisoryLease`."""

from __future__ import annotations

import pytest
from services.persistence.advisory_lease import AdvisoryLease


class FakeServer:
    """Session-level advisory locks: held by one session until it closes."""

    def __init__(self) -> None:
        self.holder: FakeConnection | None = None
        self.connections: list[FakeConnection] = []

    async def connect(self, *, server_settings=None):
        conn = FakeConnection(self, server_settings)
        self.connections.append(conn)
        return conn


class FakeConnection:
    def __init__(self, server: FakeServer, server_settings) -> None:
        self.server = server
        self.server_settings = server_settings
        self.closed = False
        self.broken = False

    async def fetchval(self, query, *args, timeout=None):
        if self.broken:
            raise ConnectionResetError("connection lost")
        if "pg_try_advisory_lock" in query:
            if self.server.holder is None or self.server.holder.closed:
                self.server.holder = self
            return self.server.holder is self
        return 1

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True


async def test_one_session_holds_the_lease_at_a_time():
    server = FakeServer()
    first = AdvisoryLease(server.connect, "openrag:canary")
    second = AdvisoryLease(server.connect, "openrag:canary")

    assert await first.acquire() is True
    assert await second.acquire() is False
    # The loser does not keep a connection open while it waits.
    assert server.connections[1].closed is True
    assert second.held is False


async def test_a_held_lease_is_confirmed_without_reconnecting():
    server = FakeServer()
    lease = AdvisoryLease(server.connect, "openrag:canary")

    assert await lease.acquire() is True
    assert await lease.acquire() is True
    assert len(server.connections) == 1


async def test_the_lease_session_tightens_tcp_keepalives():
    # A holder whose node vanished must lose the lock in minutes, not hours.
    server = FakeServer()
    await AdvisoryLease(server.connect, "openrag:canary").acquire()

    assert server.connections[0].server_settings["tcp_keepalives_idle"] == "60"


async def test_releasing_hands_the_lease_over():
    server = FakeServer()
    first = AdvisoryLease(server.connect, "openrag:canary")
    second = AdvisoryLease(server.connect, "openrag:canary")
    await first.acquire()

    await first.release()

    assert first.held is False
    assert await second.acquire() is True


async def test_a_dead_session_is_dropped_and_the_lock_contended_again():
    server = FakeServer()
    lease = AdvisoryLease(server.connect, "openrag:canary")
    await lease.acquire()
    lost = server.connections[0]
    lost.broken = True
    # Postgres ends the session, which frees the lock.
    lost.closed = True

    assert await lease.acquire() is True
    assert len(server.connections) == 2
    assert server.holder is server.connections[1]


async def test_a_connect_failure_propagates_without_holding_anything():
    async def refuse(**_):
        raise OSError("connection refused")

    lease = AdvisoryLease(refuse, "openrag:canary")

    with pytest.raises(OSError):
        await lease.acquire()
    assert lease.held is False
