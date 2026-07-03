"""Composition-root access to the TaskQueue factory.

The implementation lives in the services layer (``services.messaging.factory``)
so ``services`` consumers can build a queue without importing ``di`` (which would
invert the layer dependency). This module re-exports it for ``di``/``api``
callers that compose from the top.
"""

from __future__ import annotations

from services.messaging.factory import build_task_queue

__all__ = ["build_task_queue"]
