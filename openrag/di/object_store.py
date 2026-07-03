"""Composition-root access to the ObjectStore factory.

The implementation lives in the services layer (``services.object_store.factory``)
so ``services`` consumers can build a store without importing ``di`` (which would
invert the layer dependency). This module re-exports it for ``di``/``api`` callers
that compose from the top.
"""

from __future__ import annotations

from services.object_store.factory import build_object_store

__all__ = ["build_object_store"]
