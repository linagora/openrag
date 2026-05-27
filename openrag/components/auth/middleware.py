"""Legacy import path for :class:`AuthMiddleware` (Phase 10C shim).

The middleware now lives at :mod:`openrag.api.middleware.auth`. This module
re-exports the public surface so existing callers
(``components.auth.test_middleware``, ``chainlit_api.py``, the legacy
``openrag/main.py`` until 10G flips the entrypoint) keep working through
the strangler-fig window. Phase 12 cleanup deletes this shim.
"""

from api.middleware.auth import (
    SESSION_COOKIE_NAME,
    AuthMiddleware,
    is_bypass_path,
    is_ui_path,
)

__all__ = [
    "AuthMiddleware",
    "SESSION_COOKIE_NAME",
    "is_bypass_path",
    "is_ui_path",
]
