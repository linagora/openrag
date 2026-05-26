"""Asynchronous Postgres connection manager.

Owns a single :mod:`asyncpg` pool that every repository in
``services/persistence/`` borrows from. Lifecycle:

1. :meth:`ConnectionManager.initialize` creates the pool with a bounded
   exponential-backoff retry — the database container is often slower to
   accept connections than the app at startup.
2. :meth:`ConnectionManager.run_migrations` upgrades the schema to ``head``
   by invoking Alembic synchronously. Safe to call after ``initialize()``.
3. :meth:`ConnectionManager.shutdown` closes the pool.

The pool is exposed via the :attr:`pool` property; repositories receive a
``pool_getter`` callable so they always see the live pool even when the
manager is reinitialised in tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg
from utils.logger import get_logger

if TYPE_CHECKING:
    from core.config.infrastructure import RDBConfig


logger = get_logger()


_MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "alembic"
_ALEMBIC_INI = _MIGRATIONS_DIR / "alembic.ini"

_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 16.0


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection init: decode ``json`` / ``jsonb`` columns into Python.

    asyncpg returns JSON values as strings by default. Repositories prefer
    receiving dicts so they don't have to ``json.loads`` every row read; we
    register codecs for both flavours so the column type doesn't matter.
    """
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class ConnectionManager:
    """Lifecycle wrapper around an :class:`asyncpg.Pool`."""

    def __init__(self, config: RDBConfig) -> None:
        if not config.database:
            raise ValueError(
                "RDBConfig.database is required for ConnectionManager — "
                "set POSTGRES_DATABASE or wire it from the collection name."
            )
        # Passed to asyncpg as discrete kwargs rather than a single DSN
        # string: a password containing URL-significant characters
        # (``@ : / ? # %``) would corrupt a string DSN and silently point
        # the pool at the wrong host or fail auth.
        self._conn_kwargs = {
            "host": config.host,
            "port": config.port,
            "user": config.user,
            "password": config.password,
            "database": config.database,
        }
        # Password-free string used only for logs / error messages.
        self._dsn_log = f"postgresql://{config.user}@{config.host}:{config.port}/{config.database}"
        self._min_size = config.pool_min_size
        self._max_size = config.pool_max_size
        self._command_timeout = config.command_timeout
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError(
                "ConnectionManager.initialize() has not been called",
            )
        return self._pool

    async def initialize(self) -> None:
        """Open the pool with bounded exponential-backoff retry.

        Retries up to ``_RETRY_ATTEMPTS`` times with delays of 1, 2, 4, 8, 16
        seconds. Re-raises the final exception if every attempt fails.
        """
        if self._pool is not None:
            return

        await asyncio.to_thread(self._ensure_database_exists)

        last_exc: Exception | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    **self._conn_kwargs,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=self._command_timeout,
                    init=_init_connection,
                )
                logger.info(f"Connected to Postgres at {self._dsn_log} (pool={self._min_size}..{self._max_size})")
                return
            except (OSError, asyncpg.PostgresError) as exc:
                last_exc = exc
                if attempt == _RETRY_ATTEMPTS:
                    break
                delay = min(
                    _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                    _RETRY_MAX_DELAY_SECONDS,
                )
                logger.warning(
                    f"Postgres connection attempt {attempt}/{_RETRY_ATTEMPTS} failed ({exc}); retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        assert last_exc is not None
        raise RuntimeError(
            f"Failed to connect to Postgres at {self._dsn_log} after {_RETRY_ATTEMPTS} attempts: {last_exc}",
        ) from last_exc

    def _ensure_database_exists(self) -> None:
        """Create the configured database before opening the asyncpg pool."""
        from sqlalchemy import URL
        from sqlalchemy_utils import create_database, database_exists

        url = URL.create(
            drivername="postgresql",
            username=self._conn_kwargs["user"],
            password=self._conn_kwargs["password"],
            host=self._conn_kwargs["host"],
            port=self._conn_kwargs["port"],
            database=self._conn_kwargs["database"],
        )
        if not database_exists(url):
            create_database(url)
            logger.info(f"Created Postgres database `{self._conn_kwargs['database']}`.")

    async def shutdown(self) -> None:
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def run_migrations(self) -> None:
        """Upgrade the schema to ``head`` via Alembic.

        Alembic uses a synchronous SQLAlchemy engine, so the call is offloaded
        to the default executor to avoid blocking the event loop.
        """
        await asyncio.to_thread(self._run_migrations_sync)

    def _run_migrations_sync(self) -> None:
        # Imported lazily so the module can be imported without alembic
        # installed (e.g. for type-checking in tooling).
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import URL

        if not _ALEMBIC_INI.exists():
            raise FileNotFoundError(
                f"Alembic config not found at {_ALEMBIC_INI}; did the migrations move?",
            )

        # Build the URL through SQLAlchemy so credentials with special
        # characters are percent-encoded correctly. Alembic stores the
        # value in a ConfigParser and reads it back with ``%``-interpolation,
        # so escape literal ``%`` as ``%%`` to survive that round-trip.
        db_url = URL.create(
            "postgresql",
            username=self._conn_kwargs["user"],
            password=self._conn_kwargs["password"],
            host=self._conn_kwargs["host"],
            port=self._conn_kwargs["port"],
            database=self._conn_kwargs["database"],
        ).render_as_string(hide_password=False)

        cfg = Config(str(_ALEMBIC_INI))
        # ``script_location`` in alembic.ini is relative to the .ini file via
        # %(here)s, so this resolves to services/persistence/migrations/alembic/.
        cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
        cfg.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))
        logger.info(f"Running Alembic migrations against {self._dsn_log}")
        try:
            command.upgrade(cfg, "head")
        except Exception as exc:
            logger.error(f"Alembic migration failed for {self._dsn_log}: {exc}")
            raise RuntimeError(f"Failed to run Alembic migrations against {self._dsn_log}: {exc}") from exc
        logger.info(f"Alembic migrations completed for {self._dsn_log}")


__all__ = ["ConnectionManager"]
