"""The file-based task-log feature is gone: no route, no MCP tool, no file sink.

Task logs live in Loki (query on ``task_id``); the final error of a task stays
in ``GET /indexer/task/{task_id}`` / ``.../error``.
"""

from __future__ import annotations

from types import SimpleNamespace

from api.routers.admin.indexing import router as indexer_router
from core.utils.logging import get_logger


def test_task_logs_route_is_gone() -> None:
    paths = {route.path for route in indexer_router.routes}
    assert "/task/{task_id}/logs" not in paths
    # Neighbouring routes stay.
    assert "/task/{task_id}/error" in paths


def test_mcp_server_has_no_task_logs_tool() -> None:
    from api.mcp import server as mcp_server

    assert not hasattr(mcp_server, "get_task_logs")
    assert not hasattr(mcp_server, "LOG_FILE")


def test_get_logger_installs_only_the_stderr_sink(tmp_path, capsys) -> None:
    config = SimpleNamespace(verbose=SimpleNamespace(level="INFO"), paths=SimpleNamespace(log_dir=tmp_path))
    logger = get_logger(config)
    try:
        # Loguru keeps handlers in a private dict; one entry means one sink.
        handlers = logger._core.handlers
        assert len(handlers) == 1
        assert not (tmp_path / "app.json").exists()
        logger.info("stderr only")
        assert "stderr only" in capsys.readouterr().err
    finally:
        get_logger()  # restore the default sinks for the rest of the session
