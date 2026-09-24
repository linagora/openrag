"""Single-holder leadership over a Postgres session-level advisory lock.

For periodic work that must run once per deployment, not once per replica: the
replica that takes the lock does the work, the others keep trying and take
over when it goes away. Postgres is the one store every replica shares, in
every topology (a Uvicorn deployment with several pods has one Ray cluster per
pod, so a named Ray actor would not be unique).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg
from core.utils.logging import get_logger

logger = get_logger()

# The lock lives exactly as long as the session that took it, so a holder that
# vanished without closing its socket (node loss) keeps it until Postgres
# notices. The kernel default for that is two hours of silence; these bring it
# to about a minute and a half. Ignored on a Unix-socket connection.
_KEEPALIVE_SETTINGS = {
    "tcp_keepalives_idle": "60",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
}


class AdvisoryLease:
    """Hold, or keep trying to take, one named advisory lock.

    The lock is taken on a dedicated connection rather than a pooled one: it is
    held for as long as the process keeps the lease, and a pooled connection
    held that long is one fewer for requests.
    """

    def __init__(
        self,
        connect: Callable[..., Awaitable[asyncpg.Connection]],
        key: str,
        *,
        timeout: float = 5.0,
    ) -> None:
        self._connect = connect
        self._key = key
        self._timeout = timeout
        self._conn: asyncpg.Connection | None = None

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def acquire(self) -> bool:
        """Return whether this process holds the lease, taking it if it is free.

        Called before every unit of work: it also confirms a lease already held
        still is. A session that stopped answering has, or soon will have, lost
        its lock to Postgres, so it is dropped and the lock is contended afresh.
        """
        if self._conn is not None:
            try:
                await self._conn.fetchval("SELECT 1", timeout=self._timeout)
                return True
            except Exception as exc:
                logger.warning("Advisory lease session lost; contending again", key=self._key, error=str(exc))
                await self._discard()

        conn = await asyncio.wait_for(self._connect(server_settings=_KEEPALIVE_SETTINGS), self._timeout)
        try:
            acquired = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                self._key,
                timeout=self._timeout,
            )
        except BaseException:
            await _close_quietly(conn)
            raise
        if not acquired:
            await _close_quietly(conn)
            return False
        self._conn = conn
        logger.info("Advisory lease acquired", key=self._key)
        return True

    async def release(self) -> None:
        """Give the lease up; closing the session releases the lock."""
        if self._conn is not None:
            logger.info("Advisory lease released", key=self._key)
        await self._discard()

    async def _discard(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await _close_quietly(conn)


async def _close_quietly(conn: asyncpg.Connection) -> None:
    try:
        await asyncio.wait_for(conn.close(), timeout=5.0)
    except asyncio.CancelledError:
        conn.terminate()
        raise
    except Exception:
        conn.terminate()
