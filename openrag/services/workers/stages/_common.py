from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from core.chunking.markdown_utils import PAGE_RE
from core.models.chunk import ChunkType
from services.inference.runtime import detect_language

_CREDENTIAL_KEYS = frozenset({"credentials", "credential", "api_key", "token", "secret", "password"})

#: Language the enrichment stages write in when a file names none and its text
#: gives no confident answer.
DEFAULT_LANGUAGE = "en"
_LANGUAGE_MIN_CONFIDENCE = 0.5
_LANGUAGE_SAMPLE_CHARS = 1000
# Digits and Markdown table/markup characters carry no language and pull the
# detector towards English on number-heavy chunks.
_LANGUAGE_NOISE = re.compile(r"[\d|#*_~`>\-=+]+")
# Image captions are written in the language shown in the image, often English
# whatever the document's language, so they are left out of the sample. A
# caption cut by a chunk boundary is left out on both sides of the cut.
_IMAGE_DESCRIPTION = re.compile(r"<image_description>.*?(?:</image_description>|\Z)", re.DOTALL)
_IMAGE_DESCRIPTION_TAIL = re.compile(r"\A.*?</image_description>", re.DOTALL)
_IMAGE_PLACEHOLDER = re.compile(r"\[Image Placeholder\]", re.IGNORECASE)
# Image references left in the text when captioning is off, e.g. ``![](docling_img_3)``.
_IMAGE_REF = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
# A sample left with fewer distinct words than this, once captions are out
# (page numbers, "Fig. 3", dashes), says nothing about the language: the
# detector still answers on it, confidently and wrongly.
_LANGUAGE_WORD = re.compile(r"[^\W\d_]{2,}")
_LANGUAGE_MIN_WORDS = 3


def resolve_language(row: MutableMapping[str, Any]) -> str:
    """Language the file's contextualization and topic tags are written in.

    The upload's ``language`` metadata wins. Without it, the language is
    detected from the start of the file's text, and stored on the row so later
    stages agree. The prompts already ask for "the language of the chunk", but
    the stage passes an explicit language that overrides them: defaulting it
    to English wrote English contexts for whole non-English corpora, which
    then no longer matched their own keywords in the lexical search.
    """
    explicit = row.get("language") or row.get("lang")
    if explicit:
        return str(explicit)
    chunks = row.get("chunks") or []
    text = _language_sample(chunks, captions=False)
    language = None
    if len({word.lower() for word in _LANGUAGE_WORD.findall(text)}) >= _LANGUAGE_MIN_WORDS:
        language = detect_language(text, min_confidence=_LANGUAGE_MIN_CONFIDENCE)
    if language is None:
        language = detect_language(_language_sample(chunks, captions=True), min_confidence=_LANGUAGE_MIN_CONFIDENCE)
    row["language"] = language or DEFAULT_LANGUAGE
    return row["language"]


def _language_sample(chunks: list[Any], *, captions: bool) -> str:
    """The first ``_LANGUAGE_SAMPLE_CHARS`` of the chunks' text that carries the file's language.

    Without *captions*, image captions are left out, and so are chunks that
    hold nothing else: a file opening on figures is sampled from the text that
    follows them. With it, a file that has nothing but captions is sampled from
    them, rather than not at all.
    """
    parts: list[str] = []
    size = 0
    for chunk in chunks:
        # ``content`` is the chunk without the ``[Source | Page | Section]``
        # header structured_section writes in English in front of every chunk.
        content = getattr(chunk, "content", None)
        # CSV cells reach the chunk HTML-escaped (``&amp;``, ``&#124;``).
        text = html.unescape(str(content if content is not None else getattr(chunk, "text", "") or ""))
        if not captions:
            # A caption too long for one chunk is split into pieces that keep
            # the caption type; a middle piece carries neither marker. A caption
            # chunk can also hold short prose after the caption, so only an
            # unmarked one is dropped whole.
            if getattr(chunk, "chunk_type", None) is ChunkType.IMAGE_CAPTION and "image_description>" not in text:
                continue
            text = _IMAGE_DESCRIPTION_TAIL.sub(" ", _IMAGE_DESCRIPTION.sub(" ", text))
        for pattern in (_HTML_TAG, _IMAGE_PLACEHOLDER, _IMAGE_REF, PAGE_RE):
            text = pattern.sub(" ", text)
        text = " ".join(_LANGUAGE_NOISE.sub(" ", text).split())
        if text:
            parts.append(text)
            size += len(text) + 1
            if size > _LANGUAGE_SAMPLE_CHARS:
                break
    return " ".join(parts)[:_LANGUAGE_SAMPLE_CHARS]


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
