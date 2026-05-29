"""Source citation extraction and streaming filtering helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import re

from core.utils.logging import get_logger

logger = get_logger()

_SOURCES_NONE_RE = re.compile(
    r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?\s*none\s*\]?[.\s]*?(?=\n|$)",
    re.IGNORECASE,
)
_SOURCES_NUMS_RE = re.compile(r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?([\d,\s]+)\]?[.\s]*?(?=\n|$)")


def _strip_sources_tags(text: str) -> tuple[str, set[int], bool]:
    """Strip line-terminal source tags and return citations found."""
    cited: set[int] = set()
    for match in _SOURCES_NUMS_RE.finditer(text):
        cited.update(int(n.strip()) for n in match.group(1).split(",") if n.strip().isdigit())
    saw_none = bool(_SOURCES_NONE_RE.search(text))
    cleaned = _SOURCES_NUMS_RE.sub("", text)
    cleaned = _SOURCES_NONE_RE.sub("", cleaned)
    return cleaned, cited, saw_none


def extract_and_strip_sources_block(text: str) -> tuple[str, set[int] | None]:
    """Strip line-terminal source tags and return merged citations."""
    cleaned, citations, saw_none = _strip_sources_tags(text)

    if not citations and not saw_none:
        tail = text[-150:] if len(text) > 150 else text
        logger.debug("No [Sources: ...] tag found in LLM response", tail=repr(tail))
        return text, None

    cleaned = cleaned.rstrip()
    if citations:
        logger.debug("Extracted source citations from LLM response", citations=sorted(citations))
        return cleaned, citations

    logger.debug("LLM explicitly reported no sources used")
    return cleaned, set()


def filter_sources_by_citations(sources: list, citations: set[int] | None) -> list:
    """Keep only sources whose 1-based index was cited."""
    if citations is None:
        return sources
    if not citations:
        return []
    filtered = [source for i, source in enumerate(sources, start=1) if i in citations]
    return filtered if filtered else sources


def _min_sources_tag_buffer_size(n_sources: int) -> int:
    """Pessimistic upper bound on the length of a ``[Sources: ...]`` tag."""
    if n_sources <= 0:
        return 100
    digits_total = sum(len(str(i)) for i in range(1, n_sources + 1))
    separators = max(0, n_sources - 1) * 2
    wrapping = len("\n[Sources: ") + len("]") + 8
    return digits_total + separators + wrapping


_MIN_STREAM_LOOKAHEAD = 80


async def stream_with_source_filtering(
    llm_stream,
    sources: list,
    model_name: str,
    buffer_size: int | None = None,
):
    """Process an LLM SSE stream, stripping line-terminal source tags."""
    if buffer_size is None:
        buffer_size = max(_MIN_STREAM_LOOKAHEAD, _min_sources_tag_buffer_size(len(sources)))
    pending = ""
    emitted_len = 0
    chunk_template = None
    last_finish_reason = None

    async for line in llm_stream:
        if not line.startswith("data:"):
            continue

        if line.strip() == "data: [DONE]":
            final_clean, citations = extract_and_strip_sources_block(pending)
            final_clean = final_clean.rstrip()

            filtered = filter_sources_by_citations(sources, citations)
            filtered_json = json.dumps({"sources": filtered})

            if chunk_template and len(final_clean) > emitted_len:
                tail_chunk = copy.deepcopy(chunk_template)
                tail_chunk["choices"][0]["delta"] = {"content": final_clean[emitted_len:]}
                tail_chunk["extra"] = filtered_json
                yield f"data: {json.dumps(tail_chunk)}\n\n"

            if chunk_template:
                await asyncio.sleep(0.05)
                finish_chunk = copy.deepcopy(chunk_template)
                finish_chunk["choices"][0]["delta"] = {}
                finish_chunk["choices"][0]["finish_reason"] = last_finish_reason or "stop"
                finish_chunk["extra"] = filtered_json
                yield f"data: {json.dumps(finish_chunk)}\n\n"

            yield "data: [DONE]\n\n"
            continue

        data = json.loads(line[len("data: ") :])
        data["model"] = model_name

        choice = data.get("choices", [{}])[0]
        delta = choice.get("delta", {})
        content = delta.get("content", "") or ""
        finish_reason = choice.get("finish_reason")

        if finish_reason:
            last_finish_reason = finish_reason
            chunk_template = data
        elif content:
            chunk_template = data
            pending += content

            if len(pending) <= buffer_size:
                continue

            cleaned, _, _ = _strip_sources_tags(pending)
            safe_end = max(0, len(cleaned) - buffer_size)
            if safe_end > emitted_len:
                out = {
                    **data,
                    "choices": [
                        {
                            **choice,
                            "delta": {
                                **choice.get("delta", {}),
                                "content": cleaned[emitted_len:safe_end],
                            },
                        }
                    ],
                    "extra": "{}",
                }
                yield f"data: {json.dumps(out)}\n\n"
                emitted_len = safe_end
        else:
            data["extra"] = "{}"
            yield f"data: {json.dumps(data)}\n\n"
