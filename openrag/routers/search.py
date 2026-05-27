"""Legacy import path for the search router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.user.search`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.user.search import *  # noqa: F401,F403
from api.routers.user.search import router

__all__ = ["router"]
