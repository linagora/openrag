"""Prompt assembly helpers — pure string-formatting builders + disk loader."""

from .chat_prompt_builder import (
    EMPTY_CONTEXT_MESSAGE,
    SOURCE_SEPARATOR,
    WebSourceLike,
    format_context,
    format_web_context,
    prepend_system_prompt,
)
from .contextualization_builder import (
    BASE_CHUNK_FORMAT,
    CHUNK_FORMAT,
    wrap_chunk_with_context,
)
from .contextualization_builder import (
    build_messages as build_contextualization_messages,
)
from .contextualization_builder import (
    build_user_message as build_contextualization_user_message,
)
from .map_reduce_builder import (
    SYSTEM_PROMPT_MAP,
    USER_PROMPT_TEMPLATE,
    build_map_messages,
)
from .query_rewriter import (
    MULTI_QUERY_SEPARATOR,
    build_hyde_prompt,
    build_multi_query_prompt,
    split_multi_query_response,
)
from .template_loader import load_template, load_template_by_key
from .vlm_prompt_builder import (
    build_caption_messages,
    wrap_caption,
)

__all__ = [
    # template loader
    "load_template",
    "load_template_by_key",
    # chat
    "format_context",
    "format_web_context",
    "prepend_system_prompt",
    "SOURCE_SEPARATOR",
    "EMPTY_CONTEXT_MESSAGE",
    "WebSourceLike",
    # contextualization
    "BASE_CHUNK_FORMAT",
    "CHUNK_FORMAT",
    "build_contextualization_messages",
    "build_contextualization_user_message",
    "wrap_chunk_with_context",
    # query rewriter
    "MULTI_QUERY_SEPARATOR",
    "build_hyde_prompt",
    "build_multi_query_prompt",
    "split_multi_query_response",
    # map-reduce
    "build_map_messages",
    "SYSTEM_PROMPT_MAP",
    "USER_PROMPT_TEMPLATE",
    # VLM
    "build_caption_messages",
    "wrap_caption",
]
