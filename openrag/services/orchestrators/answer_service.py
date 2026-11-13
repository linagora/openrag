"""AnswerService: the answering step, shared by chat, streaming chat and text completions.

The sources are already chosen and rendered into the prompt when this runs;
``PreparedAnswer`` carries the request body to send, the source links shown to
the model, and what goes into ``extra`` beside them. In ``agentic`` the model
has already answered, at the end of the agent's loop: ``PreparedAnswer``
carries that reply, and ``chat`` shapes it without calling the model again,
except to retry an empty answer. AnswerService calls the
model, strips the ``[Sources: N]`` tag and keeps the cited sources, retries an
answer that comes back empty, and builds ``extra``. The rules it applies are
the pure functions of ``core.chat.answer``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.chat.answer import (
    build_extra_payload,
    empty_response_fallback,
    empty_response_retry_messages,
    is_structured_output,
    retry_empty_response_stream,
    sampling_params,
)
from core.chat.history import sanitize_messages
from core.utils.logging import get_logger
from core.utils.source_filtering import extract_and_strip_sources_block, stream_with_source_filtering

if TYPE_CHECKING:
    from core.llm.llm import LLM

logger = get_logger()


@dataclass
class PreparedAnswer:
    """What the answering step needs once the sources are known."""

    #: The request body as the model gets it: ``messages`` for chat, ``prompt`` for completions.
    payload: dict
    #: The source links presented to the model, numbered as in the prompt.
    sources: list
    #: Every retrieved source, when the request asked for ``include_all_retrieved_sources``.
    all_sources: list | None = None
    include_all_retrieved: bool = False
    #: The prompt asked the model to report its sources with a ``[Sources: N]`` tag.
    citation_protocol_active: bool = False
    retry_empty_response: bool = True
    #: Added to ``extra`` as they are, e.g. ``{"attachments": [...]}``.
    extra_fields: dict = field(default_factory=dict)
    #: Called once the answer is complete; its result is added to ``extra`` (the retrieval trace).
    finish_trace: Callable[[], dict] | None = None
    #: The model's reply when it is already made (the agent's last turn, ``agentic``): ``chat``
    #: shapes it as it would its own call's. ``None`` everywhere else.
    response: dict | None = None


class AnswerService:
    """Calls the model with a prepared prompt and shapes its answer into the response."""

    def __init__(self, *, detect_language: Callable[[str], str | None]) -> None:
        # For the language of the short message sent when the model answers nothing.
        self._detect_language = detect_language

    async def chat(self, prepared: PreparedAnswer, *, llm: LLM, model_name: str) -> dict:
        """Non-streaming chat completion → finalized OpenAI dict."""
        payload = prepared.payload
        sources = prepared.sources
        structured_output = is_structured_output(payload)

        payload["messages"] = sanitize_messages(payload["messages"])
        if prepared.response is not None:
            chunk = prepared.response
        else:
            chunk = await llm.chat(payload["messages"], **sampling_params(payload))
        chunk["model"] = model_name
        content = chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        if prepared.citation_protocol_active and not structured_output:
            clean, citations = extract_and_strip_sources_block(
                content,
                include_inline_markers=bool(sources),
            )
        else:
            clean, citations = content, None
        response_message = chunk["choices"][0]["message"]
        retry_failed = False
        can_retry = (
            prepared.retry_empty_response
            and not structured_output
            and chunk["choices"][0].get("finish_reason") in (None, "stop")
            and not clean.strip()
            and not response_message.get("tool_calls")
            and not response_message.get("refusal")
        )
        if can_retry:
            try:
                retry_chunk = await llm.chat(
                    empty_response_retry_messages(payload["messages"]), **sampling_params(payload)
                )
            except Exception as exc:
                retry_failed = True
                logger.warning("Empty-response retry failed; returning a short fallback", error=str(exc))
            else:
                retry_chunk["model"] = model_name
                retry_content = retry_chunk.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
                if prepared.citation_protocol_active:
                    clean, citations = extract_and_strip_sources_block(
                        retry_content,
                        include_inline_markers=bool(sources),
                    )
                else:
                    clean, citations = retry_content, None
                chunk = retry_chunk
                response_message = chunk["choices"][0]["message"]

        response_truncated = chunk["choices"][0].get("finish_reason") == "length"
        if (
            prepared.retry_empty_response
            and not structured_output
            and not clean.strip()
            and not response_message.get("tool_calls")
            and not response_message.get("function_call")
            and not response_message.get("refusal")
            and chunk["choices"][0].get("finish_reason") in (None, "stop", "length")
        ):
            clean = empty_response_fallback(payload["messages"], self._detect_language)
            chunk["choices"][0]["finish_reason"] = "stop"
        response_message["content"] = clean
        extra = build_extra_payload(
            sources, citations, prepared.all_sources, include_all_retrieved=prepared.include_all_retrieved
        )
        if retry_failed or response_truncated:
            extra["truncated"] = True
        extra.update(prepared.extra_fields)
        if prepared.finish_trace is not None:
            extra.update(prepared.finish_trace())
        chunk["extra"] = extra
        return chunk

    async def chat_stream(self, prepared: PreparedAnswer, *, llm: LLM, model_name: str) -> AsyncIterator[str]:
        """Streaming chat completion → SSE strings with filtered sources."""
        payload = prepared.payload
        structured_output = is_structured_output(payload)
        payload["messages"] = sanitize_messages(payload["messages"])

        def filtered_stream(messages):
            return stream_with_source_filtering(
                llm.stream_chat(messages, **sampling_params(payload)),
                prepared.sources,
                model_name,
                all_sources=prepared.all_sources,
                include_all_retrieved=prepared.include_all_retrieved,
                citation_protocol_active=prepared.citation_protocol_active and not structured_output,
                extra_fields=prepared.extra_fields or None,
                terminal_extra_fields=prepared.finish_trace,
            )

        response_stream = filtered_stream(payload["messages"])
        if prepared.retry_empty_response and not structured_output:
            fallback_extra = build_extra_payload(
                prepared.sources, None, prepared.all_sources, include_all_retrieved=prepared.include_all_retrieved
            )
            fallback_extra.update(prepared.extra_fields)
            response_stream = retry_empty_response_stream(
                response_stream,
                lambda: filtered_stream(empty_response_retry_messages(payload["messages"])),
                fallback_text=empty_response_fallback(payload["messages"], self._detect_language),
                fallback_extra=fallback_extra,
                model_name=model_name,
            )
        async for sse_line in response_stream:
            yield sse_line

    async def complete(self, prepared: PreparedAnswer, *, llm: LLM) -> dict:
        """Non-streaming text completion → finalized OpenAI dict. No empty-answer retry here."""
        payload = prepared.payload
        structured_output = is_structured_output(payload)

        resp = await llm.generate(payload["prompt"], **sampling_params(payload, key="prompt"))
        text = resp.get("choices", [{}])[0].get("text", "") or ""
        if prepared.citation_protocol_active and not structured_output:
            clean, citations = extract_and_strip_sources_block(
                text,
                include_inline_markers=bool(prepared.sources),
            )
        else:
            clean, citations = text, None
        resp["choices"][0]["text"] = clean
        extra = build_extra_payload(
            prepared.sources, citations, prepared.all_sources, include_all_retrieved=prepared.include_all_retrieved
        )
        extra.update(prepared.extra_fields)
        if prepared.finish_trace is not None:
            extra.update(prepared.finish_trace())
        resp["extra"] = extra
        return resp


__all__ = ["AnswerService", "PreparedAnswer"]
