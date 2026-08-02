"""Source citation extraction and streaming filtering helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import re

from core.utils.logging import get_logger

logger = get_logger()

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_SOURCES_NONE_RE = re.compile(
    r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?\s*none\s*\]?[.\s]*?(?=\n|$)",
    re.IGNORECASE,
)
_SOURCES_NUMS_RE = re.compile(r"\n?[ \t]*\[?Sources?\]?\s*:\s*\[?([\d,\s]+)\]?[.\s]*?(?=\n|$)", re.IGNORECASE)
_INLINE_SOURCE_NUMS_RE = re.compile(
    r"[ \t]*\[\s*Sources?\s+(\d+(?:\s*,\s*\d+)*)\s*\]",
    re.IGNORECASE,
)
_UNCLOSED_SOURCE_NUMS_RE = re.compile(
    r"[ \t]*\[\s*Sources?\s+(\d+(?:\s*,\s*\d+)*)\s*(?=\n|$)",
    re.IGNORECASE,
)
_DANGLING_SOURCE_RE = re.compile(r"[ \t]*\[\s*Sources?\s*(?=\n|$)", re.IGNORECASE)


def _sanitize_log_preview(text: str, max_length: int = 150) -> str:
    preview = _EMAIL_RE.sub("***@***", text)
    if len(preview) > max_length:
        preview = preview[-max_length:]
    return preview


def _strip_sources_tags(text: str, *, include_inline_markers: bool = True) -> tuple[str, set[int], bool]:
    """Strip source tags and return citations found."""
    cited: set[int] = set()
    patterns = [_SOURCES_NUMS_RE]
    if include_inline_markers:
        patterns.extend((_INLINE_SOURCE_NUMS_RE, _UNCLOSED_SOURCE_NUMS_RE))
    for pattern in patterns:
        for match in pattern.finditer(text):
            cited.update(int(n.strip()) for n in match.group(1).split(",") if n.strip().isdigit())
    saw_none = bool(_SOURCES_NONE_RE.search(text))
    cleaned = _SOURCES_NUMS_RE.sub("", text)
    cleaned = _SOURCES_NONE_RE.sub("", cleaned)
    if include_inline_markers:
        cleaned = _INLINE_SOURCE_NUMS_RE.sub("", cleaned)
        cleaned = _UNCLOSED_SOURCE_NUMS_RE.sub("", cleaned)
        cleaned = _DANGLING_SOURCE_RE.sub("", cleaned)
    return cleaned, cited, saw_none


def extract_and_strip_sources_block(
    text: str,
    *,
    include_inline_markers: bool = True,
) -> tuple[str, set[int] | None]:
    """Strip source tags and return merged citations."""
    cleaned, citations, saw_none = _strip_sources_tags(text, include_inline_markers=include_inline_markers)

    if not citations and not saw_none:
        if cleaned != text:
            logger.debug("Removed incomplete source marker from LLM response")
            return cleaned.rstrip(), None
        tail = text[-150:] if len(text) > 150 else text
        logger.debug("No [Sources: ...] tag found in LLM response", tail=repr(_sanitize_log_preview(tail)))
        return text, None

    cleaned = cleaned.rstrip()
    if citations:
        logger.debug("Extracted source citations from LLM response", citations=sorted(citations))
        return cleaned, citations

    logger.debug("LLM explicitly reported no sources used")
    return cleaned, set()


def filter_sources_by_citations(sources: list, citations: set[int] | None) -> list:
    """Keep only sources whose 1-based index was cited.

    No tag at all (``citations is None``) means the model didn't report which
    sources it used, not that it used none — the answer may still be grounded
    in them, so keep everything rather than silently dropping real sources.
    """
    if citations is None:
        return sources
    if not citations:
        return []
    return [source for i, source in enumerate(sources, start=1) if i in citations]


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
    *,
    citation_protocol_active: bool = True,
):
    """Process an LLM SSE stream and, when active, strip source tags.

    The terminal flush (tail content + ``extra.sources``) runs exactly once
    after the loop on *every* termination path — a clean ``data: [DONE]``, the
    upstream closing the connection without one, or the upstream generator
    raising mid-stream (e.g. a read timeout surfaced as ``InferenceTimeoutError``).
    Otherwise a dropped upstream stream silently loses the answer's tail and all
    sources with no error surfaced to the caller. A non-clean exit is flagged
    with ``extra.truncated = true`` so the client can tell a cut-off answer from
    a clean completion; if nothing was ever streamed there is no tail to salvage,
    so a mid-stream error is re-raised to surface the real failure instead of a
    silent empty ``[DONE]``.
    """
    if buffer_size is None:
        buffer_size = max(_MIN_STREAM_LOOKAHEAD, _min_sources_tag_buffer_size(len(sources)))
    include_inline_markers = citation_protocol_active and bool(sources)
    pending = ""
    emitted_len = 0
    chunk_template = None
    # Fallback template for the truncated-finish chunk when the stream dies
    # before any content/finish chunk (e.g. only a role-preamble arrived): any
    # chunk with a usable `choices[0]` lets us still emit the `truncated` flag.
    last_chunk = None
    last_finish_reason = None
    saw_done = False
    stream_error = None

    try:
        async for line in llm_stream:
            if not line.startswith("data:"):
                continue

            if line.strip() == "data: [DONE]":
                saw_done = True
                break

            data = json.loads(line[len("data: ") :])
            data["model"] = model_name

            # `choices` can be present but empty — e.g. the OpenAI/litellm final
            # usage-report chunk sent when the caller requests
            # `stream_options: {"include_usage": true}` has `"choices": []` and a
            # top-level `"usage"` field. `.get("choices", [{}])` only falls back to
            # the default when the key is *missing*, not when it's an empty list,
            # so indexing straight into it raises IndexError on that chunk.
            choices = data.get("choices") or [{}]
            choice = choices[0]
            delta = choice.get("delta", {})
            content = delta.get("content", "") or ""
            finish_reason = choice.get("finish_reason")

            # Only chunks with a real `choices[0]` can template the finish chunk;
            # skip usage-report chunks (`"choices": []`) whose deepcopy would
            # IndexError in the flush below.
            if data.get("choices"):
                last_chunk = data

            # `content` and `finish_reason` are handled independently: some
            # OpenAI-compatible providers pack the last token and the terminal
            # `finish_reason` into the *same* chunk, so gating content on
            # `elif finish_reason` would silently drop that final token.
            if finish_reason:
                last_finish_reason = finish_reason
                chunk_template = data

            if content:
                chunk_template = data
                pending += content

                if len(pending) <= buffer_size:
                    continue

                if citation_protocol_active:
                    cleaned, _, _ = _strip_sources_tags(
                        pending,
                        include_inline_markers=include_inline_markers,
                    )
                else:
                    cleaned = pending
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
                                # `choice` may carry finish_reason (a provider can pack
                                # the last token + finish_reason into one chunk). Clear
                                # it: a mid-stream chunk marked terminal makes spec
                                # clients drop the delta and ignore the tail/finish
                                # chunks. Same guard as the terminal tail chunk below.
                                "finish_reason": None,
                            }
                        ],
                        "extra": "{}",
                    }
                    yield f"data: {json.dumps(out)}\n\n"
                    emitted_len = safe_end
            elif not finish_reason:
                # Neither content nor finish_reason (role preamble, usage-only or
                # keep-alive chunk): pass it through untouched. A finish-only chunk
                # is intentionally *not* re-emitted here — the terminal flush emits
                # the finish chunk so it can carry `extra.sources`.
                data["extra"] = "{}"
                yield f"data: {json.dumps(data)}\n\n"
    except Exception as exc:
        # Upstream raised mid-stream (timeout, connection drop, worker restart):
        # capture it and fall through to the flush so the buffered tail + sources
        # are still delivered instead of unwinding past it. CancelledError and
        # GeneratorExit are BaseException, not Exception, so a downstream client
        # disconnect propagates untouched and correctly skips the flush.
        stream_error = exc
    finally:
        # Release the upstream HTTP connection promptly. Breaking on `[DONE]`
        # leaves the client's stream generator suspended inside its
        # `async with response` (an `async for` does *not* close its iterator on
        # break), so the pooled connection stays checked out until GC — which
        # exhausts the httpx pool under concurrent traffic. Closing it here runs
        # that cleanup now. Awaiting in a finally is safe (only *yielding* during
        # teardown is forbidden); the guard tolerates iterables without `aclose`.
        aclose = getattr(llm_stream, "aclose", None)
        if aclose is not None:
            await aclose()

    # The finish chunk needs *a* template; prefer the content/finish chunk, but
    # fall back to any earlier chunk so a preamble-only stream still gets flagged.
    template = chunk_template or last_chunk

    # Nothing was ever streamed to the client, so there is no partial answer to
    # salvage. Surface the real error instead of a silent, clean-looking empty
    # `[DONE]` the caller can't distinguish from success.
    if stream_error is not None and template is None:
        logger.warning("Upstream stream raised before any content; surfacing error", error=str(stream_error))
        raise stream_error

    if citation_protocol_active:
        final_clean, citations = extract_and_strip_sources_block(
            pending,
            include_inline_markers=include_inline_markers,
        )
        final_clean = final_clean.rstrip()
    else:
        final_clean, citations = pending, None

    filtered = filter_sources_by_citations(sources, citations)
    extra_payload = {"sources": filtered, "retrieved_sources": sources}
    if not saw_done:
        extra_payload["truncated"] = True
        logger.warning(
            "Answer truncated: upstream stream ended without [DONE] "
            "(reason={reason}, model={model}, delivered_chars={chars}, sources={sources})",
            reason=f"upstream error: {stream_error}" if stream_error is not None else "connection closed",
            model=model_name,
            chars=len(final_clean),
            sources=len(filtered),
        )
    filtered_json = json.dumps(extra_payload)

    if template and len(final_clean) > emitted_len:
        tail_chunk = copy.deepcopy(template)
        tail_chunk["choices"][0]["delta"] = {"content": final_clean[emitted_len:]}
        # Content chunk must not carry finish_reason (template may be the
        # finish chunk); clients treat such a chunk as terminal and drop its
        # delta. The separate finish chunk below emits it with an empty delta.
        tail_chunk["choices"][0]["finish_reason"] = None
        tail_chunk["extra"] = filtered_json
        yield f"data: {json.dumps(tail_chunk)}\n\n"

    if template:
        await asyncio.sleep(0.05)
        finish_chunk = copy.deepcopy(template)
        finish_chunk["choices"][0]["delta"] = {}
        finish_chunk["choices"][0]["finish_reason"] = last_finish_reason or "stop"
        finish_chunk["extra"] = filtered_json
        yield f"data: {json.dumps(finish_chunk)}\n\n"

    yield "data: [DONE]\n\n"
