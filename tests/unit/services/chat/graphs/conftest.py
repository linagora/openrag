"""Fixtures shared by the chat graph tests."""

from __future__ import annotations

import pytest
from core.utils.logging import get_logger


@pytest.fixture
def logged_steps():
    """The ``Chat step`` lines logged while the test runs, each as ``(step, details)``."""
    steps: list[tuple[str, dict]] = []

    def sink(message) -> None:
        record = message.record
        if record["message"] == "Chat step":
            details = {k: v for k, v in record["extra"].items() if k not in ("graph", "step")}
            steps.append((f"{record['extra']['graph']}:{record['extra']['step']}", details))

    logger = get_logger()
    handle = logger.add(sink, level="DEBUG")
    try:
        yield steps
    finally:
        logger.remove(handle)
