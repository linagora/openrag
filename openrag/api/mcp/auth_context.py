"""Request-scoped auth context for the MCP server.

FastMCP tool callables receive only their declared arguments — there is no
FastAPI ``Request`` to thread the authenticated principal through. The
``MCPAuthContextMiddleware`` resolves the caller once per request and stashes
the principal here; the tool functions read it and forward it explicitly to
``MCPService`` (which never touches this module — it stays a pure
orchestrator).
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_USER_ID: ContextVar[int | None] = ContextVar("openrag_mcp_user_id", default=None)
_IS_ADMIN: ContextVar[bool] = ContextVar("openrag_mcp_is_admin", default=False)
_ALLOWED_PARTITIONS: ContextVar[list[str] | None] = ContextVar("openrag_mcp_partitions", default=None)

AuthTokens = tuple[Token, Token, Token]


def set_auth_context(
    *,
    user_id: int | None,
    is_admin: bool,
    allowed_partitions: list[str] | None,
) -> AuthTokens:
    return (
        _USER_ID.set(user_id),
        _IS_ADMIN.set(is_admin),
        _ALLOWED_PARTITIONS.set(allowed_partitions),
    )


def reset_auth_context(tokens: AuthTokens) -> None:
    user_token, admin_token, partitions_token = tokens
    _USER_ID.reset(user_token)
    _IS_ADMIN.reset(admin_token)
    _ALLOWED_PARTITIONS.reset(partitions_token)


def get_user_id() -> int | None:
    return _USER_ID.get()


def is_admin() -> bool:
    return _IS_ADMIN.get()


def get_allowed_partitions() -> list[str] | None:
    return _ALLOWED_PARTITIONS.get()
