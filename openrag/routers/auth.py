"""Legacy import path for the auth router (Phase 10F shim).

The router now lives at :mod:`openrag.api.routers.auth.oidc`. The
current codebase has no token-based login endpoint — every route in
this file is part of the OIDC flow plus the protected ``/auth/me``
debug endpoint — so the Phase 10F split keeps everything in a single
``oidc.py`` module rather than the ``login.py + oidc.py`` pair the
strategy doc envisioned (which assumed token-login content that does
not yet exist in OpenRAG).

This module re-exports the public surface so ``openrag/main.py`` and
``openrag/routers/test_auth_router.py`` keep working through the
strangler-fig window; Phase 12 cleanup deletes this shim.
"""

from api.routers.auth.oidc import *  # noqa: F401,F403
from api.routers.auth.oidc import router

__all__ = ["router"]
