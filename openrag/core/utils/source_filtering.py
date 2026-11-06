"""Source citation extraction and streaming filtering helpers."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import unicodedata
from collections.abc import Callable

from core.utils.logging import get_logger

logger = get_logger()

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
# Models copy the prompt's markdown literally (`[Sources: none]` in backticks,
# **Sources:** in bold) and move the emphasis around freely: outside the
# brackets, inside them around the label ([**Sources:** none],
# [**Sources**]: 1, 3), or around the value ([Sources: **1, 3**],
# [Sources: **1**, **3**]). So emphasis/code marks are accepted at every
# boundary of the tag, and are part of it when it is stripped.
#
# Every run of marks or whitespace below is possessive, so a failed search never
# retries the ways of splitting a run between neighbours (a long run of marks or
# spaces after the value made it quadratic). That loses no match: what follows a
# run either cannot start with one of its characters or is another run that
# would take the same ones, so giving any back could not help. The lookbehinds
# enter the leading whitespace and mark runs only at their first character, for
# the same reason: a match starting mid-run is never needed, starting at the run
# covers it. After the value, whitespace stays on the tag's line unless a
# closing bracket follows, which is what the end-of-line lookahead needs anyway.
#
# The label up to its colon, with marks allowed before and after each bracket
# and around "Sources".
_SOURCES_LABEL = r"\n?(?<![ \t])[ \t]*+(?<![*_`])[*_`]*+\[?[*_`]*+Sources?[*_`]*+\]?[*_`]*+\s*+:\s*+[*_`]*+\[?[ \t]*+"
# Any whitespace but a newline may end the line, as the final rstrip() removes
# all of it: a no-break space after a tag must not hide the tag from the strip.
_SOURCES_TAG_TAIL = r"(?:[.*_`]|[^\S\n])*+(?=\n|$)"
# The closing bracket may sit on the next line ([Sources: 1, 3,\n]); without
# one, the tag ends on its own line.
_SOURCES_TAG_CLOSE = r"[,\s*_`]*+\]"
_SOURCES_TAG_END = r"(?:" + _SOURCES_TAG_CLOSE + r"|[,*_` \t]*+)" + _SOURCES_TAG_TAIL
_SOURCES_NONE_RE = re.compile(
    _SOURCES_LABEL + r"[*_`]*+none" + _SOURCES_TAG_END,
    re.IGNORECASE,
)
# Each number may carry its own marks and separators ([Sources: **1**, **3**]),
# and a long list may wrap onto the next line. The repetition itself is not
# possessive: when what follows the last number is not the end of the tag
# ("Sources: 1\n2. Next item"), it gives numbers back one at a time, which
# costs one short scan each since every separator run stops at the next digit.
_SOURCES_NUMS_RE = re.compile(
    _SOURCES_LABEL + r"((?:[,\s*_`]*+\d++)+)" + _SOURCES_TAG_END,
    re.IGNORECASE,
)
# An empty tag ([Sources: ]) cites nothing but is still stripped. It needs its
# closing bracket: a bare "Sources:" at the end of a line may just be prose.
_SOURCES_EMPTY_RE = re.compile(
    _SOURCES_LABEL + _SOURCES_TAG_CLOSE + _SOURCES_TAG_TAIL,
    re.IGNORECASE,
)
# Inline markers ([Source 2], [Sources 1, 3]) take the whitespace before them,
# entered at its first character like the label above.
_INLINE_SOURCE_NUMS_RE = re.compile(
    r"(?<![ \t])[ \t]*+\[\s*Sources?\s+(\d+(?:\s*,\s*\d+)*)\s*\]",
    re.IGNORECASE,
)
_UNCLOSED_SOURCE_NUMS_RE = re.compile(
    r"(?<![ \t])[ \t]*+\[\s*Sources?\s+(\d+(?:\s*,\s*\d+)*)\s*(?=\n|$)",
    re.IGNORECASE,
)
_DANGLING_SOURCE_RE = re.compile(r"(?<![ \t])[ \t]*+\[\s*Sources?\s*(?=\n|$)", re.IGNORECASE)


def _sanitize_log_preview(text: str, max_length: int = 150) -> str:
    preview = _EMAIL_RE.sub("***@***", text)
    if len(preview) > max_length:
        preview = preview[-max_length:]
    return preview


# No source index has more digits than this, and int() refuses a string of more
# than 4,300 digits, which a model can still write.
_MAX_CITATION_DIGITS = 9


def _citation_index(digits: str) -> int:
    """The 1-based source index ``digits`` cites, or 0, which matches no source, if too long to be one."""
    # ``\d`` matches every Unicode decimal digit, and int() reads them all, so a
    # zero is any digit whose value is 0, not only the ASCII one.
    significant = "".join(str(unicodedata.decimal(c)) for c in digits).lstrip("0")
    if len(significant) > _MAX_CITATION_DIGITS:
        return 0
    return int(significant or "0")


# Removing a tag can expose another: once an inline marker or a trailing
# [Sources: none] is gone, the [Sources: 1, 3] before it ends its line
# ("... [Sources: 1, 3][Source 1]"). So stripping repeats until nothing changes.
# Each round scans the whole text and a chain of k adjacent tags takes k rounds,
# so the rounds are capped: past the cap, the first tags of a longer chain stay.
_MAX_STRIP_ROUNDS = 4


def _reads_as_a_tag(match: re.Match[str], starts_line: bool) -> bool:
    """Whether a label that a removed tag left at the end of its line is a tag, not prose.

    A bracket, a mark or a line of its own makes it one ("**Sources:** 1",
    "Answer.\n\nSources: 1, 2"). Without them it is the answer's own words
    ("Nombre de sources : 3 [Source 2]"), which stay, their number uncounted.
    *starts_line* says whether the text itself begins a line.
    """
    tag = match.group(0)
    if tag.startswith("\n") or any(mark in tag for mark in "[*_`"):
        return True
    return match.start() == 0 and starts_line


def _strip_sources_tags(
    text: str,
    *,
    include_inline_markers: bool = True,
    starts_line: bool = True,
) -> tuple[str, set[int], bool]:
    """Strip source tags and return citations found."""
    cited: set[int] = set()
    saw_none = False
    exposed = False

    def take_inline_numbers(match: re.Match[str]) -> str:
        # Every number the tag holds is a citation, whatever separates them:
        # the regexes accept spaces, newlines and marks as well as commas
        # ([Sources: **1** **3**]), and a tag stripped with its numbers
        # unread would fall back to citing every source.
        cited.update(_citation_index(n) for n in re.findall(r"\d+", match.group(1)))
        return ""

    def take_numbers(match: re.Match[str]) -> str:
        if exposed and not _reads_as_a_tag(match, starts_line):
            return match.group(0)
        return take_inline_numbers(match)

    def take_none(match: re.Match[str]) -> str:
        nonlocal saw_none
        if exposed and not _reads_as_a_tag(match, starts_line):
            return match.group(0)
        saw_none = True
        return ""

    # Citations are read from the text each pattern is applied to, so a tag
    # exposed by an earlier substitution is both stripped and counted.
    substitutions = [(_SOURCES_NUMS_RE, take_numbers), (_SOURCES_NONE_RE, take_none), (_SOURCES_EMPTY_RE, "")]
    if include_inline_markers:
        substitutions += [
            (_INLINE_SOURCE_NUMS_RE, take_inline_numbers),
            (_UNCLOSED_SOURCE_NUMS_RE, take_inline_numbers),
            (_DANGLING_SOURCE_RE, ""),
        ]
    cleaned = text
    for _ in range(_MAX_STRIP_ROUNDS):
        stripped = cleaned
        for pattern, replacement in substitutions:
            stripped = pattern.sub(replacement, stripped)
        if stripped == cleaned:
            break
        cleaned = stripped
        exposed = True
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

# Up to its last character that no tag or marker can contain: no character of
# the regexes above, no letter of "sources" or "none". Stripping never removes
# such a character and no lookbehind rejects it, so stripping the text up to it
# and the text after it separately gives the same result as stripping the whole.
# Outside ASCII that is any character but a space or a digit (\s and \d match
# them) and "ſ", which IGNORECASE matches as "s": without them, an answer in
# another script has no such character and streams in bursts.
_LAST_TAG_FREE_CHAR_RE = re.compile(
    r".*(?:[abdf-mpqtv-zABDF-MPQTV-Z!\"#$%&'()+\-/;<=>?@^{|}~]|(?![\s\d\u017f])[^\x00-\x7f])",
    re.DOTALL,
)


def _settled_length(tail: str, cleaned: str, include_inline_markers: bool) -> int:
    """How much of the stripped ``tail`` stays the same whatever text follows.

    That is the stripped text up to the last tag-free character. Past it, more
    text can still change the result: a blank run or a chain of tags at the end
    of the text is stripped while it ends the line and kept once the line goes on.
    """
    match = _LAST_TAG_FREE_CHAR_RE.match(tail)
    if match is None:
        return 0
    rest, _, _ = _strip_sources_tags(
        tail[match.end() :],
        include_inline_markers=include_inline_markers,
        starts_line=False,
    )
    return len(cleaned) - len(rest)


def _fold_point(
    tail: str,
    cleaned: str,
    emitted: int,
    keep: int,
    include_inline_markers: bool,
) -> tuple[int, int] | None:
    """Where the streamed ``tail`` can be cut, keeping at least ``keep`` characters.

    Returns the cut and the length of the stripped text before it, or None when
    there is no such character or the text before it is not all emitted yet.
    """
    match = _LAST_TAG_FREE_CHAR_RE.match(tail, 0, max(0, len(tail) - keep))
    if match is None:
        return None
    rest, _, _ = _strip_sources_tags(
        tail[match.end() :],
        include_inline_markers=include_inline_markers,
        starts_line=False,
    )
    folded = len(cleaned) - len(rest)
    if folded > emitted:
        return None
    return match.end(), folded


async def stream_with_source_filtering(
    llm_stream,
    sources: list,
    model_name: str,
    buffer_size: int | None = None,
    *,
    citation_protocol_active: bool = True,
    all_sources: list | None = None,
    include_all_retrieved: bool = False,
    extra_fields: dict | None = None,
    terminal_extra_fields: dict | Callable[[], dict] | None = None,
):
    """Process an LLM SSE stream and, when active, strip source tags.

    ``sources`` is the prompt-visible (context-budget-truncated) list used to
    resolve citation indices — reported as-is via ``extra.sources`` (legacy,
    kept for existing clients: cited sources, or every presented source as a
    fallback when no tag was found) and ``extra.presented_sources`` (always
    the raw pre-filter list, so a client can render "sources consulted" when
    nothing was cited). ``extra.cited_sources`` is the strict version: only
    what the model actually cited, empty whenever no tag was found — never
    falling back to "everything" the way ``sources`` does.
    ``extra.citations_reported`` disambiguates those two empty/full states
    on the wire: ``true`` only when a ``[Sources: ...]`` tag (even an empty
    one) was actually found.

    ``all_sources`` — the complete pre-truncation retrieval set — is reported
    as ``extra.all_retrieved_sources`` only when ``include_all_retrieved`` is
    true (it defaults to ``sources`` when the caller has nothing more
    complete to offer). It's gated because a full retrieval dump on every
    response is debug/eval telemetry, not something most callers need on the
    hot path.

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
    # Stripping all of `pending` at every delta would be quadratic in the answer
    # length. Only `tail`, its end, is stripped: what is cut off its front (at a
    # `_fold_point`, once emitted) is counted in `folded_len`. Where there is
    # nowhere to cut, or the text before the cut is not emitted yet, `tail` is
    # stripped again only once it has grown by an eighth, which keeps the total
    # linear. Each delta still strips a tail a few lookaheads long, and the
    # lookahead grows with the number of sources, so the cost per delta does too.
    tail = ""
    folded_len = 0
    # The first tail begins the answer; a folded one begins past a character on its line.
    tail_starts_line = True
    stripped_len = 0
    stuck = False
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
                tail += content

                if len(pending) <= buffer_size:
                    continue

                if citation_protocol_active:
                    if stuck and len(tail) - stripped_len < len(tail) // 8:
                        continue
                    cleaned, _, _ = _strip_sources_tags(
                        tail,
                        include_inline_markers=include_inline_markers,
                        starts_line=tail_starts_line,
                    )
                    stripped_len = len(tail)
                    # Emitted text cannot be taken back, so only what is settled is emitted.
                    settled = _settled_length(tail, cleaned, include_inline_markers)
                else:
                    cleaned = tail
                    settled = len(cleaned)
                safe_end = max(0, folded_len + min(settled, len(cleaned) - buffer_size))
                if safe_end > emitted_len:
                    out = {
                        **data,
                        "choices": [
                            {
                                **choice,
                                "delta": {
                                    **choice.get("delta", {}),
                                    "content": cleaned[emitted_len - folded_len : safe_end - folded_len],
                                },
                                # `choice` may carry finish_reason (a provider can pack
                                # the last token + finish_reason into one chunk). Clear
                                # it: a mid-stream chunk marked terminal makes spec
                                # clients drop the delta and ignore the tail/finish
                                # chunks. Same guard as the terminal tail chunk below.
                                "finish_reason": None,
                            }
                        ],
                        "extra": {},
                    }
                    yield f"data: {json.dumps(out)}\n\n"
                    emitted_len = safe_end
                if citation_protocol_active and len(tail) > 4 * buffer_size:
                    fold = _fold_point(
                        tail,
                        cleaned,
                        emitted_len - folded_len,
                        2 * buffer_size,
                        include_inline_markers,
                    )
                    stuck = fold is None
                    if fold is not None:
                        cut, folded = fold
                        tail = tail[cut:]
                        tail_starts_line = False
                        folded_len += folded
                        stripped_len = len(tail)
            elif not finish_reason:
                # Neither content nor finish_reason (role preamble, usage-only or
                # keep-alive chunk): pass it through untouched. A finish-only chunk
                # is intentionally *not* re-emitted here — the terminal flush emits
                # the finish chunk so it can carry `extra.sources`.
                data["extra"] = {}
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
    extra_payload = {
        "sources": filtered,
        "presented_sources": sources,
        "cited_sources": filtered if citations is not None else [],
        "citations_reported": citations is not None,
    }
    if include_all_retrieved:
        extra_payload["all_retrieved_sources"] = all_sources if all_sources is not None else sources
    if extra_fields:
        extra_payload.update(extra_fields)
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

    if template and len(final_clean) > emitted_len:
        tail_chunk = copy.deepcopy(template)
        tail_chunk["choices"][0]["delta"] = {"content": final_clean[emitted_len:]}
        # Content chunk must not carry finish_reason (template may be the
        # finish chunk); clients treat such a chunk as terminal and drop its
        # delta. The separate finish chunk below emits it with an empty delta.
        tail_chunk["choices"][0]["finish_reason"] = None
        tail_chunk["extra"] = extra_payload
        yield f"data: {json.dumps(tail_chunk)}\n\n"

    finish_extra = None
    if template or terminal_extra_fields:
        finish_extra = dict(extra_payload)
        if terminal_extra_fields:
            try:
                finalized_fields = terminal_extra_fields() if callable(terminal_extra_fields) else terminal_extra_fields
                finish_extra.update(finalized_fields)
            except Exception as error:  # noqa: BLE001 - optional telemetry must not break the answer stream
                logger.bind(error_type=type(error).__name__).warning(
                    "Terminal telemetry could not be finalized and was omitted."
                )

    if template:
        await asyncio.sleep(0.05)
        finish_chunk = copy.deepcopy(template)
        finish_chunk["choices"][0]["delta"] = {}
        finish_chunk["choices"][0]["finish_reason"] = last_finish_reason or "stop"
        finish_chunk["extra"] = finish_extra
        yield f"data: {json.dumps(finish_chunk)}\n\n"
    elif finish_extra is not None:
        finish_chunk = {
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "extra": finish_extra,
        }
        yield f"data: {json.dumps(finish_chunk)}\n\n"

    yield "data: [DONE]\n\n"
