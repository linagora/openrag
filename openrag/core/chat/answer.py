"""Shaping the model's answer into the response: citations, the empty-answer retry, ``extra``.

Pure functions over messages, payloads and SSE lines; the service that calls
the model (``services.orchestrators.answer_service``) wires them together.

An answer that is empty once the citation tag is stripped is retried once with
``EMPTY_RESPONSE_RETRY_INSTRUCTION`` appended to the system prompt; if the retry
is empty too, or fails, the client gets ``empty_response_fallback``'s short
message instead of nothing. Streaming does the same in
``retry_empty_response_stream``, holding back the stream until it shows text.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import aclosing

from core.chat.smalltalk import acknowledgement_language, is_casual_reply
from core.utils.logging import get_logger
from core.utils.source_filtering import filter_sources_by_citations

logger = get_logger()


EMPTY_RESPONSE_RETRY_INSTRUCTION = (
    "Your previous response was empty after response filtering. Try once more using the conversation, "
    "the supplied context, and the response language already specified above. If the context does not support a "
    "substantive answer, say so briefly. Do not return only a source marker or an empty message."
)


def empty_response_retry_messages(messages: list[dict]) -> list[dict]:
    """Append the retry instruction to the existing system message when present."""
    retry_messages = [dict(message) for message in messages]
    if retry_messages and retry_messages[0].get("role") == "system":
        existing = retry_messages[0].get("content") or ""
        retry_messages[0]["content"] = f"{existing.rstrip()}\n\n{EMPTY_RESPONSE_RETRY_INSTRUCTION}"
    else:
        retry_messages.insert(0, {"role": "system", "content": EMPTY_RESPONSE_RETRY_INSTRUCTION})
    return retry_messages


def empty_response_fallback(messages: list[dict], detect_language: Callable[[str], str | None]) -> str:
    """Return a short, language-aware response when one retry cannot answer.

    *detect_language* returns the language of a text, or ``None`` when unsure.
    """
    latest_user = next(
        (message.get("content") or "" for message in reversed(messages) if message.get("role") == "user"),
        "",
    )
    is_casual = is_casual_reply(latest_user)
    language = (
        acknowledgement_language(messages, latest_user, detect_language)
        if is_casual
        else detect_language(latest_user) or "en"
    )
    if language == "fr":
        if is_casual:
            return "Je suis là si vous souhaitez continuer."
        return "Je n'ai pas pu formuler de réponse. Veuillez réessayer."
    if is_casual:
        return "I'm here if you'd like to continue."
    return "I couldn't produce a response. Please try again."


def _fallback_stream_lines(template, fallback_text: str, extra: dict, model_name: str):
    """Build a complete SSE response around a short fallback message."""
    base = dict(template or {})
    base.setdefault("model", model_name)
    choices = base.get("choices") or [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    original_choice = dict(choices[0])
    base["choices"] = [{**original_choice, "delta": {"content": fallback_text}, "finish_reason": None}]
    base["extra"] = {key: value for key, value in extra.items() if key != "retrieval_trace"}
    yield f"data: {json.dumps(base)}\n\n"

    finish = dict(template or base)
    finish.setdefault("model", model_name)
    finish_choice = dict((finish.get("choices") or [original_choice])[0])
    finish["choices"] = [{**finish_choice, "delta": {}, "finish_reason": "stop"}]
    finish["extra"] = dict(extra)
    yield f"data: {json.dumps(finish)}\n\n"
    yield "data: [DONE]\n\n"


async def retry_empty_response_stream(
    initial_stream,
    retry_stream_factory,
    *,
    fallback_text: str,
    fallback_extra: dict,
    model_name: str,
):
    """Retry once after source filtering proves a completed answer is empty."""
    template = None
    extra = dict(fallback_extra)

    for attempt in range(2):
        buffered: list[str] = []
        answer_started = False
        saw_done = False
        saw_non_text_output = False
        saw_truncated = False
        last_finish_reason = None

        try:
            stream = initial_stream if attempt == 0 else retry_stream_factory()
            async with aclosing(stream):
                async for line in stream:
                    if answer_started:
                        yield line
                        continue

                    buffered.append(line)
                    if not line.startswith("data:"):
                        continue
                    data_text = line[len("data:") :].strip()
                    if data_text == "[DONE]":
                        saw_done = True
                        break

                    try:
                        data = json.loads(data_text)
                    except json.JSONDecodeError:
                        # Filtered streams should be valid SSE JSON; pass unexpected
                        # events through instead of treating them as an empty answer.
                        answer_started = True
                        for buffered_line in buffered:
                            yield buffered_line
                        buffered.clear()
                        continue

                    event_extra = data.get("extra")
                    if isinstance(event_extra, dict) and event_extra:
                        extra = dict(event_extra)
                        saw_truncated = saw_truncated or event_extra.get("truncated") is True

                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    template = data
                    choice = choices[0]
                    delta = choice.get("delta", {}) or {}
                    content = delta.get("content", "") or ""
                    if choice.get("finish_reason") is not None:
                        last_finish_reason = choice["finish_reason"]
                    if delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                        saw_non_text_output = True
                    if (isinstance(content, str) and content.strip()) or saw_non_text_output:
                        answer_started = True
                        for buffered_line in buffered:
                            yield buffered_line
                        buffered.clear()
        except Exception as exc:
            if answer_started or attempt == 0:
                raise
            logger.warning("Empty-response stream retry failed; returning a short fallback", error=str(exc))
            fallback_extra = {**extra, "truncated": True}
            for fallback_line in _fallback_stream_lines(template, fallback_text, fallback_extra, model_name):
                yield fallback_line
            return

        if answer_started:
            return
        if attempt == 0 and saw_truncated:
            for buffered_line in buffered:
                yield buffered_line
            return
        if attempt == 0 and saw_done and not saw_truncated and last_finish_reason in (None, "stop"):
            continue

        if last_finish_reason not in (None, "stop", "length"):
            for buffered_line in buffered:
                yield buffered_line
            return

        if attempt == 1 and saw_truncated:
            extra = {**extra, "truncated": True}
        if last_finish_reason == "length":
            extra = {**extra, "truncated": True}
        for fallback_line in _fallback_stream_lines(template, fallback_text, extra, model_name):
            yield fallback_line
        return


def sampling_params(payload: dict, key: str = "messages") -> dict:
    """Sampling kwargs handed to the core LLM (everything but the body).

    Mirrors the legacy ``_LLMShim``: strip the transport keys; the core
    ``VLLMClient`` consumes ``metadata`` (llm_override) and the rest as
    OpenAI sampling params.
    """
    drop = {key, "stream", "model"}
    return {k: v for k, v in payload.items() if k not in drop}


def is_structured_output(payload: dict) -> bool:
    """Structured output cannot carry the plain-text citation marker."""
    response_format = payload.get("response_format")
    return isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}


def build_extra_payload(
    sources: list,
    citations: set[int] | None,
    all_sources: list | None,
    *,
    include_all_retrieved: bool,
) -> dict:
    """The ``extra`` shape every answer shares, streamed or not.

    It mirrors ``stream_with_source_filtering``'s payload, minus the
    streaming-only ``truncated`` flag, so the response paths can't drift apart.
    """
    filtered = filter_sources_by_citations(sources, citations)
    payload = {
        "sources": filtered,
        "presented_sources": sources,
        "cited_sources": filtered if citations is not None else [],
        "citations_reported": citations is not None,
    }
    if include_all_retrieved:
        payload["all_retrieved_sources"] = all_sources
    return payload


__all__ = [
    "EMPTY_RESPONSE_RETRY_INSTRUCTION",
    "build_extra_payload",
    "empty_response_fallback",
    "empty_response_retry_messages",
    "is_structured_output",
    "retry_empty_response_stream",
    "sampling_params",
]
