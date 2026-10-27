from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from services.inference.runtime import detect_language

_CREDENTIAL_KEYS = frozenset({"credentials", "credential", "api_key", "token", "secret", "password"})

#: Language the enrichment stages write in when a file names none and its text
#: gives no confident answer.
DEFAULT_LANGUAGE = "en"
_LANGUAGE_MIN_CONFIDENCE = 0.5
_LANGUAGE_SAMPLE_CHUNKS = 5
_LANGUAGE_SAMPLE_CHARS = 1000
# Digits and Markdown table/markup characters carry no language and pull the
# detector towards English on number-heavy chunks.
_LANGUAGE_NOISE = re.compile(r"[\d|#*_~`>\-=+]+")


def resolve_language(row: MutableMapping[str, Any]) -> str:
    """Language the file's contextualization and topic tags are written in.

    The upload's ``language`` metadata wins. Without it, the language is
    detected from the first chunks, and stored on the row so later stages
    agree. The prompts already ask for "the language of the chunk", but the
    stage passes an explicit language that overrides them: defaulting it to
    English wrote English contexts for whole non-English corpora, which then
    no longer matched their own keywords in the lexical search.
    """
    explicit = row.get("language") or row.get("lang")
    if explicit:
        return str(explicit)
    chunks = row.get("chunks") or []
    sample = " ".join(str(getattr(chunk, "text", "") or "") for chunk in chunks[:_LANGUAGE_SAMPLE_CHUNKS])
    sample = " ".join(_LANGUAGE_NOISE.sub(" ", sample).split())
    language = detect_language(sample[:_LANGUAGE_SAMPLE_CHARS], min_confidence=_LANGUAGE_MIN_CONFIDENCE)
    row["language"] = language or DEFAULT_LANGUAGE
    return row["language"]


def scrub_credentials(row: MutableMapping[str, Any]) -> None:
    for key in _CREDENTIAL_KEYS:
        row.pop(key, None)


def stage_timeout(base_timeout: float | None, item_count: int, *, per_item_timeout: float = 0.0) -> float | None:
    if base_timeout is None:
        return None
    return base_timeout + max(0, item_count) * per_item_timeout


async def run_with_optional_timeout[T](
    operation: Callable[[], Awaitable[T]],
    timeout: float | None,
) -> T:
    if timeout is None:
        return await operation()
    return await asyncio.wait_for(operation(), timeout=timeout)
