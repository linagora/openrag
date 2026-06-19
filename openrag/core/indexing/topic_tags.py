"""Document-level topic tagging for indexed chunks."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence

from core.llm import LLM
from core.models.chunk import Chunk

logger = logging.getLogger(__name__)

_MAX_PROMPT_CHARS = 8000


class TopicTagger:
    """Extract a compact set of document topic labels with an LLM."""

    def __init__(
        self,
        llm: LLM,
        system_prompt: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._timeout = timeout_seconds

    async def tag(
        self,
        chunks: Sequence[Chunk],
        *,
        filename: str = "",
        max_tags: int = 7,
        lang: str = "en",
    ) -> list[str]:
        """Return normalized, unique topic tags for a document."""
        chunks = list(chunks)
        if not chunks:
            return []

        try:
            messages = _build_messages(
                system_prompt=self._system_prompt,
                chunks=chunks,
                filename=filename,
                max_tags=max_tags,
                lang=lang,
            )
            operation = self._llm.chat(messages)
            response = await asyncio.wait_for(operation, timeout=self._timeout) if self._timeout else await operation
            return _parse_topic_tags(_chat_response_text(response), max_tags=max_tags)
        except (TimeoutError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning("Error extracting topic tags for %s: %s", filename, exc)
            return []


def _build_messages(
    *,
    system_prompt: str,
    chunks: Sequence[Chunk],
    filename: str,
    max_tags: int,
    lang: str,
) -> list[dict[str, str]]:
    chunk_text = "\n\n".join(
        f"Chunk {index + 1}:\n{chunk.text}" for index, chunk in enumerate(chunks[:12]) if chunk.text.strip()
    )
    chunk_text = chunk_text[:_MAX_PROMPT_CHARS]
    user_prompt = (
        f"Filename: {filename or 'unknown'}\n"
        f"Language: {lang}\n"
        f"Maximum topics: {max_tags}\n\n"
        "Document chunks:\n"
        f"{chunk_text}\n\n"
        "Return only a JSON array of short topic strings."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _parse_topic_tags(text: str, *, max_tags: int) -> list[str]:
    parsed = _load_json_array(text)
    if parsed is None:
        return []

    tags: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, str):
            continue
        tag = _normalize_display_tag(item)
        key = tag.casefold()
        if not tag or key in seen:
            continue
        tags.append(tag)
        seen.add(key)
        if len(tags) >= max_tags:
            break
    return tags


def _load_json_array(text: str) -> list[object] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("topics"), list):
        return value["topics"]
    if isinstance(value, dict) and isinstance(value.get("tags"), list):
        return value["tags"]
    return None


def _normalize_display_tag(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:80]


def _chat_response_text(response: object) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return ""

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(first.get("text"), str):
                return first["text"]

    content = response.get("content")
    return content if isinstance(content, str) else ""


__all__ = ["TopicTagger"]
