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
from typing import Any

from core.utils.logging import get_logger

logger = get_logger()

# Long enough to recognise a prompt by its opening sentence, short enough that a
# retrieval context of a dozen documents stays one readable line.
PREVIEW_CHARS = 240


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= PREVIEW_CHARS else f"{flat[:PREVIEW_CHARS]}…"


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
        for part in content:
            if not isinstance(part, dict):
                parts.append(_preview(str(part)))
            elif part.get("type") == "text":
                parts.append(_preview(str(part.get("text", ""))))
            else:
                parts.append(f"<{part.get('type', 'unknown')}>")
        return " + ".join(parts)
    return _preview(json.dumps(content, ensure_ascii=False, default=str))


def _describe(message: Any) -> str:
    if not isinstance(message, dict):
        return _preview(str(message))
    role = str(message.get("role", "?"))
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
    """

    def _detail() -> str:
        if messages is not None:
            return " || ".join(_describe(m) for m in messages)
        text = prompt or ""
        return f"prompt[{len(text)}]: {_preview(text)}"

    # The lazy preview is passed positionally on purpose: loguru copies **kwargs
    # into ``record["extra"]``, and the terminal formatter appends every extra —
    # so a ``detail=`` kwarg would print the whole payload a second time on each
    # line. Positional args are formatted into the message only.
    logger.bind(caller=caller, model=model, endpoint=endpoint, stream=stream).opt(lazy=True).debug(
        f"llm.call {caller} model={model} stream={stream} | " + "{}",
        _detail,
    )
