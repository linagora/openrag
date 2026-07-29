"""DEBUG-level logging of the prompts that actually reach an LLM.

``PromptService._log_resolution`` proves *which* library prompt each pipeline
stage resolved; this proves the resolved text is what landed in the outbound
request body — the other half of the wiring check. One ``llm.call`` line per
request, emitted only when ``LOG_LEVEL=DEBUG``, with every message previewed
rather than dumped so a context-stuffed chat (or a base64 image) cannot flood
the log.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()

# Long enough to recognise a prompt by its opening sentence, short enough that a
# retrieval context of a dozen documents stays one readable line.
PREVIEW_CHARS = 240
# A long chat history or a multi-image request would otherwise join into one
# unbounded line, so the whole record is bounded too — not just each fragment.
MAX_MESSAGES = 12
MAX_PARTS = 6
MAX_DETAIL_CHARS = 2000
# Identifiers interpolated into the message. ``model`` is client-controllable
# (metadata.llm_override), so it is flattened and bounded like the rest.
MAX_META_CHARS = 120

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def _redact(text: str) -> str:
    """Pseudonymize email addresses before any prompt text reaches a log sink.

    Prompts carry user questions and retrieved document context, both of which
    routinely contain addresses; the diagnostic value of this line is the prompt
    *shape*, never the personal data inside it.
    """
    return _EMAIL.sub("<email>", text)


def _clip(text: str, limit: int) -> str:
    """Truncate to at most *limit* characters, ellipsis included.

    The ellipsis counts against the budget rather than being appended past it,
    so every cap here is the real ceiling on the emitted length — otherwise each
    clipped span silently ran one character over its limit.
    """
    if limit <= 0:
        return ""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _preview(text: str) -> str:
    return _clip(_redact(" ".join(text.split())), PREVIEW_CHARS)


def _meta(value: object) -> str:
    """Flatten a value that is interpolated into the log message.

    Newlines in a client-supplied model name would otherwise let a caller forge
    additional log lines, so every identifier is collapsed to one line and
    bounded before it is formatted or bound.
    """
    return _clip(" ".join(str(value).split()), MAX_META_CHARS)


def _render_content(content: Any) -> str:
    """Flatten one message's ``content`` to a previewable string.

    Multimodal content arrives as a list of parts whose image entries carry a
    base64 data URI — those are reduced to a type marker so image bytes never
    reach the log.
    """
    if isinstance(content, str):
        return _preview(content)
    if isinstance(content, list):
        parts = []
        for part in content[:MAX_PARTS]:
            if not isinstance(part, dict):
                parts.append(_preview(str(part)))
            elif part.get("type") == "text":
                parts.append(_preview(str(part.get("text", ""))))
            else:
                parts.append(f"<{_meta(part.get('type', 'unknown'))}>")
        if len(content) > MAX_PARTS:
            parts.append(f"(+{len(content) - MAX_PARTS} more parts)")
        return " + ".join(parts)
    return _preview(json.dumps(content, ensure_ascii=False, default=str))


def _describe(message: Any) -> str:
    if not isinstance(message, dict):
        return _preview(str(message))
    role = _meta(message.get("role", "?"))
    content = message.get("content")
    body = _render_content(content)
    size = len(content) if isinstance(content, str) else len(body)
    return f"{role}[{size}]: {body}"


def log_llm_call(
    *,
    caller: str,
    model: str,
    endpoint: str,
    messages: list | None = None,
    prompt: str | None = None,
    stream: bool = False,
) -> None:
    """Emit one ``llm.call`` line describing an outbound request.

    The previews are built inside a lazily-evaluated argument, so callers pay
    nothing for this when the sink level is above DEBUG. Retries log per
    attempt, which is deliberate — a retried call is a real second request.

    The whole record is bounded: per-message previews, a cap on how many
    messages and multimodal parts are rendered, and a final clamp on the joined
    result, so no request can turn one call into an unbounded log line.
    """
    safe_caller, safe_model, safe_endpoint = _meta(caller), _meta(model), _meta(endpoint)

    def _detail() -> str:
        if messages is not None:
            rendered = [_describe(m) for m in messages[:MAX_MESSAGES]]
            if len(messages) > MAX_MESSAGES:
                rendered.append(f"(+{len(messages) - MAX_MESSAGES} more messages)")
            return _clip(" || ".join(rendered), MAX_DETAIL_CHARS)
        text = prompt or ""
        return _clip(f"prompt[{len(text)}]: {_preview(text)}", MAX_DETAIL_CHARS)

    # The lazy preview is passed positionally on purpose: loguru copies **kwargs
    # into ``record["extra"]``, and the terminal formatter appends every extra —
    # so a ``detail=`` kwarg would print the whole payload a second time on each
    # line. Positional args are formatted into the message only.
    logger.bind(caller=safe_caller, model=safe_model, endpoint=safe_endpoint, stream=stream).opt(lazy=True).debug(
        f"llm.call {safe_caller} model={safe_model} stream={stream} | " + "{}",
        _detail,
    )
