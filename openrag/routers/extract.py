"""Legacy import path for the extract router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.user.extract`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.user.extract import *  # noqa: F401,F403
from api.routers.user.extract import router

__all__ = ["router"]
