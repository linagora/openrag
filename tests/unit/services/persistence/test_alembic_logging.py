"""Running the migrations from a program must leave its logging as it was.

Alembic's ``env.py`` configures logging from ``alembic.ini`` with ``fileConfig()``,
which replaces the root logger's handlers and level and disables every logger
created before it. That suits the ``alembic`` command, not the app, which runs
the migrations at startup after its own logging is set up. These tests run the
real ``env.py`` offline, so no database is needed: the first migration then stops
on an inspection, which shows that ``env.py`` ran. ``fileConfig`` is spied on, so
a real one never changes this test session's logging.
"""

import io
import logging
import logging.config

import pytest
from alembic import command
from alembic.config import main as alembic_main
from loguru import logger
from services.persistence import connection
from services.persistence.connection import ConnectionManager
from sqlalchemy.exc import NoInspectionAvailable


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
def file_config_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(logging.config, "fileConfig", lambda path, *args, **kwargs: calls.append(path))
    return calls


@pytest.fixture
def app_logging():
    """An app-style root handler, an early logger, and the ``alembic`` logger, all restored."""
    root = logging.getLogger()
    alembic_logger = logging.getLogger("alembic")
    saved = (list(root.handlers), root.level, list(alembic_logger.handlers), alembic_logger.level)
    saved_propagate = alembic_logger.propagate
    handler = logging.StreamHandler(io.StringIO())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    early = logging.getLogger("test.alembic_logging.early")
    try:
        yield handler, early
    finally:
        root.handlers, root.level = saved[0], saved[1]
        alembic_logger.handlers, alembic_logger.level = saved[2], saved[3]
        alembic_logger.propagate = saved_propagate


async def _run_app_migrations_offline(monkeypatch) -> BaseException | None:
    real_upgrade = command.upgrade
    monkeypatch.setattr(command, "upgrade", lambda cfg, revision: real_upgrade(cfg, revision, sql=True))
    with pytest.raises(RuntimeError) as failure:
        await ConnectionManager(RDBConfigStub()).run_migrations()
    return failure.value.__cause__


async def test_app_migrations_keep_the_app_logging(monkeypatch, capsys, file_config_calls, app_logging):
    handler, early = app_logging

    cause = await _run_app_migrations_offline(monkeypatch)

    assert isinstance(cause, NoInspectionAvailable), "env.py did not run its migrations"
    assert file_config_calls == []
    root = logging.getLogger()
    assert handler in root.handlers
    assert root.level == logging.INFO
    assert early.disabled is False


async def test_app_migrations_send_alembic_progress_to_the_app_log(monkeypatch, capsys, file_config_calls, app_logging):
    """Without alembic.ini's console handler, ``Running upgrade …`` must still reach loguru."""
    records: list[dict] = []
    sink_id = logger.add(lambda message: records.append(message.record), level="INFO")
    try:
        await _run_app_migrations_offline(monkeypatch)
    finally:
        logger.remove(sink_id)

    progress = [r for r in records if "Running upgrade" in r["message"]]
    assert progress
    assert progress[0]["extra"]["logger_name"] == "alembic.runtime.migration"


def test_the_alembic_command_still_configures_logging_from_its_ini(capsys, file_config_calls):
    with pytest.raises(NoInspectionAvailable):
        alembic_main(argv=["-c", str(connection._ALEMBIC_INI), "upgrade", "head", "--sql"])

    assert file_config_calls == [str(connection._ALEMBIC_INI)]
