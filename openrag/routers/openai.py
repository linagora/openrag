"""Legacy import path for the OpenAI-compatible chat router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.user.chat`. This module
re-exports the public surface so ``openrag/main.py`` and any external
importers keep working through the strangler-fig window; Phase 12
cleanup deletes this shim.
"""

from api.routers.user.chat import *  # noqa: F401,F403
from api.routers.user.chat import router

__all__ = ["router"]
