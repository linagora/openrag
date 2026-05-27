"""Legacy import path for the partition router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.admin.partitions`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.admin.partitions import *  # noqa: F401,F403
from api.routers.admin.partitions import router

__all__ = ["router"]
