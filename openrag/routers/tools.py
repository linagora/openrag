"""Legacy import path for the tools router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.admin.tools`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.admin.tools import *  # noqa: F401,F403
from api.routers.admin.tools import router

__all__ = ["router"]
