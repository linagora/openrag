"""The token budget of a chat or text-completion request, checked before anything runs.

The caller's own input (messages, tool definitions, prompt) plus the output it
asks for must fit in the window of the endpoint that will answer
(``api.dependencies.model_limits``); otherwise the request is refused with a
413 before retrieval or the model is called. Retrieved context is capped
separately, inside what ``max_prompt_tokens_for`` leaves. Shared by the chat
completions, text completions and retrieval-diagnostics routes.
"""

import traceback
from typing import TYPE_CHECKING

from api.dependencies.model_limits import effective_max_output_tokens, get_max_model_tokens
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest
from core.config import load_config
from core.config.endpoints import client_llm_override, custom_endpoint_override_enabled
from core.prompts import message_tokens, tool_definition_tokens
from core.utils.exceptions import ServiceUnavailableError
from core.utils.logging import get_logger
from core.utils.text import get_num_tokens
from fastapi import HTTPException, status

if TYPE_CHECKING:
    from core.config.root import Settings

logger = get_logger()


def _runtime_config(settings: "Settings | None" = None) -> "Settings":
    return settings if settings is not None else load_config()


def _token_validation_unavailable(error: Exception) -> ServiceUnavailableError:
    # Counter exceptions can contain input text or credentials. Log only the
    # type and stack locations; the raised error chains the original cause.
    logger.warning(
        "Token validation unavailable",
        error_type=type(error).__name__,
        traceback=[
            f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(error.__traceback__)
        ],
    )
    return ServiceUnavailableError(
        "Unable to validate request token limit. Try again.",
        code="TOKEN_VALIDATION_UNAVAILABLE",
    )


def validate_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    max_tokens_allowed: int,
    settings: "Settings | None" = None,
    partitions: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate the token budget, raising 503 if counting is unavailable."""
    config = _runtime_config(settings)
    try:
        counter = get_num_tokens()
    except Exception as error:
        raise _token_validation_unavailable(error) from error

    def _length_function(text: str) -> int:
        try:
            return counter(text)
        except Exception as error:
            raise _token_validation_unavailable(error) from error

    if isinstance(request, OpenAIChatCompletionRequest):
        body = request.model_dump(exclude_none=True)
        messages_tokens = sum(message_tokens(m, _length_function) for m in body["messages"])
        tools_tokens = tool_definition_tokens(body, _length_function)
        default_output_tokens = effective_max_output_tokens(config, partitions)
        requested_tokens = request.max_tokens or default_output_tokens
        total_tokens_needed = messages_tokens + tools_tokens + requested_tokens
        if total_tokens_needed > max_tokens_allowed:
            tools_part = f"Tool definitions: {tools_tokens} tokens + " if tools_tokens else ""
            return False, (
                f"Request exceeds maximum token limit. "
                f"Messages: {messages_tokens} tokens + "
                f"{tools_part}"
                f"Requested output: {requested_tokens} tokens = "
                f"{total_tokens_needed} tokens. "
                f"Maximum allowed: {max_tokens_allowed} tokens."
            )

    elif isinstance(request, OpenAICompletionRequest):
        prompt_tokens = _length_function(request.prompt)
        default_output_tokens = effective_max_output_tokens(config, partitions)
        requested_tokens = request.max_tokens or default_output_tokens
        total_tokens_needed = prompt_tokens + requested_tokens
        if total_tokens_needed > max_tokens_allowed:
            return False, (
                f"Request exceeds maximum token limit. "
                f"Prompt: {prompt_tokens} tokens + "
                f"Requested output: {requested_tokens} tokens = "
                f"{total_tokens_needed} tokens. "
                f"Maximum allowed: {max_tokens_allowed} tokens."
            )

    return True, ""


def apply_default_max_tokens(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    config: "Settings",
    partitions: list[str] | None,
) -> None:
    """Fill an omitted ``max_tokens`` from the endpoint that will actually answer.

    The request schema leaves ``max_tokens`` unset because it is parsed before
    the partition is resolved; defaulting it there could only ever read the
    *default* endpoint's budget, so a partition whose ``chat_llm`` endpoint
    allows more output would still be capped at the default endpoint's value
    (and that value was then sent downstream, since the payload forwards
    ``max_tokens`` verbatim). Resolving it here — once the partition, and
    therefore the answering endpoint, is known — keeps the output budget
    consistent with the endpoint that serves the request.

    An explicit client-supplied value is always honoured.

    Skipped for a client-supplied endpoint: this budget describes the *server's*
    endpoint, and the client's provider may reject ``max_tokens`` outright (newer
    OpenAI models want ``max_completion_tokens``). Left unset it drops from the
    payload; ``validate_tokens_limit`` still falls back to the configured default.
    """
    if request.max_tokens is not None:
        return
    llm_override = client_llm_override(getattr(request, "metadata", None))
    if llm_override.get("base_url") and custom_endpoint_override_enabled():
        return
    request.max_tokens = effective_max_output_tokens(config, partitions)


def max_prompt_tokens_for(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    config: "Settings",
    partitions: list[str] | None,
) -> int | None:
    """Tokens the prompt may take on the endpoint answering this request.

    Its context window (``get_max_model_tokens``: admin-set, else probed, else
    the global fallback) minus the output budget. ``QueryService`` caps the
    retrieved context to what is left of it once the instructions and the
    conversation are in. ``None`` for a client-supplied endpoint, whose window
    is unknown here.

    Called after ``apply_default_max_tokens``, which fills ``max_tokens`` for
    every request this function doesn't return ``None`` for.
    """
    llm_override = client_llm_override(getattr(request, "metadata", None))
    if llm_override.get("base_url") and custom_endpoint_override_enabled():
        return None
    return get_max_model_tokens(partitions=partitions, settings=config) - request.max_tokens


def check_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    log,
    settings: "Settings | None" = None,
    partitions: list[str] | None = None,
):
    """Validate token limit and raise HTTPException(413) if exceeded."""
    is_valid, error_message = validate_tokens_limit(
        request,
        max_tokens_allowed=get_max_model_tokens(partitions=partitions, settings=settings),
        settings=settings,
        partitions=partitions,
    )
    if not is_valid:
        log.info("Request exceeds token limit", detail=error_message)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=error_message,
        )


__all__ = ["apply_default_max_tokens", "check_tokens_limit", "max_prompt_tokens_for", "validate_tokens_limit"]
