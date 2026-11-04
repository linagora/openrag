"""Running the migrations from the app must leave the app's logging as it was.

Alembic's ``env.py`` configures logging from ``alembic.ini`` with ``fileConfig()``,
which replaces the root logger's handlers and level and disables every logger
created before it. That suits the ``alembic`` CLI, not the app, which runs the
migrations at startup after its own logging is set up. These tests run the real
``env.py``, offline so no database is needed: the migrations themselves then
fail on their first inspection, after ``env.py`` has done its logging setup.
"""

import contextlib
import io
import logging

import pytest
from alembic import command
from alembic.config import Config
from services.persistence import connection
from services.persistence.connection import ConnectionManager


class RDBConfigStub:
    host = "127.0.0.1"
    port = 1
    user = "u"
    password = "p"
    database = "db"
    auto_create_database = False
    pool_min_size = 1
    pool_max_size = 1
    command_timeout = 1


@pytest.fixture
def app_logging():
    """An app-style root handler and an early logger, restored afterwards."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_disabled = {
        name: lg.disabled for name, lg in logging.root.manager.loggerDict.items() if isinstance(lg, logging.Logger)
    }
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    early = logging.getLogger("test.alembic_logging.early")
    try:
        yield handler, early
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, lg in logging.root.manager.loggerDict.items():
            if isinstance(lg, logging.Logger):
                lg.disabled = saved_disabled.get(name, False)


def _upgrade_offline(monkeypatch):
    real_upgrade = command.upgrade
    monkeypatch.setattr(command, "upgrade", lambda cfg, revision: real_upgrade(cfg, revision, sql=True))


@pytest.mark.asyncio
async def test_app_migrations_keep_the_app_logging(monkeypatch, capsys, app_logging):
    handler, early = app_logging
    _upgrade_offline(monkeypatch)

    with contextlib.suppress(RuntimeError):
        await ConnectionManager(RDBConfigStub()).run_migrations()

    root = logging.getLogger()
    assert handler in root.handlers
    assert root.level == logging.INFO
    assert early.disabled is False


def test_the_alembic_cli_still_configures_logging_from_its_ini(capsys, app_logging):
    """Without the app's flag, env.py keeps configuring logging, as the CLI expects."""
    handler, early = app_logging
    cfg = Config(str(connection._ALEMBIC_INI))
    cfg.set_main_option("script_location", str(connection._MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://u:p@127.0.0.1:1/db")

    with contextlib.suppress(Exception):
        command.upgrade(cfg, "head", sql=True)

    assert handler not in logging.getLogger().handlers
    assert early.disabled is True
