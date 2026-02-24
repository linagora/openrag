from contextvars import ContextVar, Token

_USER_ID: ContextVar[int | None] = ContextVar("openrag_mcp_user_id", default=None)
_PARTITIONS: ContextVar[list[str] | None] = ContextVar("openrag_mcp_partitions", default=None)


def set_auth_context(user_id: int | None, partitions: list[str] | None) -> tuple[Token, Token]:
    user_token = _USER_ID.set(user_id)
    partitions_token = _PARTITIONS.set(partitions)
    return user_token, partitions_token


def reset_auth_context(tokens: tuple[Token, Token]) -> None:
    user_token, partitions_token = tokens
    _USER_ID.reset(user_token)
    _PARTITIONS.reset(partitions_token)


def get_user_id() -> int | None:
    return _USER_ID.get()


def get_allowed_partitions() -> list[str] | None:
    return _PARTITIONS.get()
