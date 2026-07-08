"""CORS origin sanitisation.

A wildcard origin combined with credentials is unsafe: Starlette's
``CORSMiddleware`` reflects the request ``Origin`` *with* credentials in that
case, which defeats the allowlist and lets any site make credentialed calls.
This drops any ``"*"`` when credentials are enabled so a misconfigured
``CORS_EXTRA_ORIGINS=*`` cannot silently open that hole.
"""

from __future__ import annotations

from core.utils.logging import get_logger

logger = get_logger()


def sanitize_cors_origins(origins: list[str], *, allow_credentials: bool) -> list[str]:
    """Return ``origins`` with any wildcard removed when credentials are on.

    When ``allow_credentials`` is ``False`` the list is returned unchanged (a
    wildcard is safe without credentials). When it is ``True`` and a ``"*"`` is
    present, the wildcard is dropped and a warning is logged.
    """
    if not allow_credentials or "*" not in origins:
        return origins
    logger.warning(
        "Dropping wildcard '*' from CORS origins because credentials are enabled; "
        "list explicit origins in CORS_EXTRA_ORIGINS"
    )
    return [origin for origin in origins if origin != "*"]


__all__ = ["sanitize_cors_origins"]
