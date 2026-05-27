"""Legacy import path for the actors router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.admin.cluster`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.admin.cluster import *  # noqa: F401,F403
from api.routers.admin.cluster import router

__all__ = ["router"]
