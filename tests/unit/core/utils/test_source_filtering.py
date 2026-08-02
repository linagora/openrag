"""Tests for source citation extraction and filtering utilities."""

import asyncio
import json

import pytest
from core.utils.source_filtering import (
    _min_sources_tag_buffer_size,
    _sanitize_log_preview,
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    stream_with_source_filtering,
)


class TestExtractAndStripSourcesBlock:
    def test_basic_extraction(self):
        text = "Answer text\n[Sources: 1, 3]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_single_source(self):
        text = "Answer text\n[Source: 2]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {2}

    def test_many_sources(self):
        text = "Answer text\n[Sources: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}

    def test_no_sources_block(self):
        text = "Answer with no block"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer with no block"
        assert citations is None

    def test_sources_with_trailing_whitespace(self):
        text = "Answer text\n[Sources: 1, 3]   "
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_sources_with_extra_spaces(self):
        text = "Answer text\n[Sources:  1 ,  3 , 5 ]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3, 5}

    def test_multiline_answer(self):
        text = "Line 1\n\nLine 2\n\nLine 3\n[Sources: 2, 4]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Line 1\n\nLine 2\n\nLine 3"
        assert citations == {2, 4}

    def test_empty_string(self):
        clean, citations = extract_and_strip_sources_block("")
        assert clean == ""
        assert citations is None

    def test_sources_mid_text_not_stripped(self):
        text = "Answer [Sources: 1, 2] and more text after"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == text
        assert citations is None

    def test_brackets_around_numbers_only(self):
        text = "Answer text\nSources: [1, 3]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_no_brackets_at_all(self):
        text = "Answer text\nSources: 1, 3"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_singular_no_brackets(self):
        text = "Answer text\nSource: 2"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {2}

    def test_sources_none(self):
        text = "Answer text\n[Sources: none]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()

    def test_sources_none_no_brackets(self):
        text = "Answer text\nSources: none"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()

    def test_sources_none_capitalized(self):
        text = "Answer text\n[Sources: None]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == set()

    def test_sources_numbers_case_insensitive(self):
        text = "Answer text\n[sources: 1, 3]"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Answer text"
        assert citations == {1, 3}

    def test_log_preview_redacts_email_addresses(self):
        preview = _sanitize_log_preview("Contact alice@example.com for details")
        assert "alice@example.com" not in preview
        assert "***@***" in preview

    def test_multiple_line_terminal_tags_stripped(self):
        """Bullet-leak case: LLM emits [Sources: X] per bullet item instead of once at end."""
        text = "- Claim one about the codebase.\n[Sources: 1]\n- Claim two about APEX.\n[Sources: 1, 5]\n"
        clean, citations = extract_and_strip_sources_block(text)
        assert "[Sources:" not in clean
        assert citations == {1, 5}

    def test_tag_at_end_of_sentence_followed_by_more_lines(self):
        """Tag terminating a sentence (not the response) is stripped; following content preserved."""
        text = "The project uses Ray. [Sources: 2, 3]\nAnother paragraph here."
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "The project uses Ray.\nAnother paragraph here."
        assert citations == {2, 3}

    def test_tag_inline_in_prose_preserved(self):
        """Meta-discussion: the tag appears inside a sentence and must NOT be stripped."""
        text = "Use the format [Sources: 1, 3] at the very end of your response."
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == text
        assert citations is None

    def test_context_source_markers_are_recovered_and_stripped(self):
        text = "The footprint fell by 28% [Source 7].\nThe partners include Flexis [Source 8][Source 9]."
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "The footprint fell by 28%.\nThe partners include Flexis."
        assert citations == {7, 8, 9}

    def test_unclosed_numbered_source_marker_is_recovered(self):
        text = "The target is 2040 [Source 2"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "The target is 2040"
        assert citations == {2}

    def test_dangling_source_marker_is_removed_without_a_citation(self):
        text = "Logistics emissions fell by 30% [Source"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == "Logistics emissions fell by 30%"
        assert citations is None


class TestFilterSourcesByCitations:
    def test_basic_filtering(self):
        sources = ["a", "b", "c", "d", "e"]
        result = filter_sources_by_citations(sources, {1, 3, 5})
        assert result == ["a", "c", "e"]

    def test_none_citations_returns_all_sources(self):
        """No tag at all means the model didn't report citations, not that it used none."""
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, None)
        assert result == ["a", "b", "c"]

    def test_empty_citations_returns_empty(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, set())
        assert result == []

    def test_out_of_range_citations_returns_empty(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, {99})
        assert result == []

    def test_partial_out_of_range(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, {1, 99})
        assert result == ["a"]

    def test_single_citation(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, {2})
        assert result == ["b"]

    def test_empty_sources(self):
        result = filter_sources_by_citations([], {1, 2})
        assert result == []

    def test_all_cited(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, {1, 2, 3})
        assert result == ["a", "b", "c"]

    def test_preserves_order(self):
        sources = ["a", "b", "c", "d"]
        result = filter_sources_by_citations(sources, {4, 2})
        assert result == ["b", "d"]

    def test_with_dict_sources(self):
        sources = [{"file": "a.pdf"}, {"file": "b.pdf"}, {"file": "c.pdf"}]
        result = filter_sources_by_citations(sources, {1, 3})
        assert result == [{"file": "a.pdf"}, {"file": "c.pdf"}]


# --- helpers for streaming tests ---


def _make_chunk(content: str, chunk_id: str = "chatcmpl-1") -> str:
    """Build an SSE line with a content delta."""
    return "data: " + json.dumps({"id": chunk_id, "choices": [{"delta": {"content": content}, "finish_reason": None}]})


def _make_finish(chunk_id: str = "chatcmpl-1") -> str:
    """Build an SSE line with finish_reason='stop'."""
    return "data: " + json.dumps({"id": chunk_id, "choices": [{"delta": {}, "finish_reason": "stop"}]})


def _make_content_finish(content: str, chunk_id: str = "chatcmpl-1") -> str:
    """Build a terminal SSE line carrying both a content delta and finish_reason.

    Some OpenAI-compatible providers pack the last token and the stop reason into
    the same final chunk.
    """
    return "data: " + json.dumps(
        {"id": chunk_id, "choices": [{"delta": {"content": content}, "finish_reason": "stop"}]}
    )


DONE_LINE = "data: [DONE]"


def _make_preamble(chunk_id: str = "chatcmpl-1") -> str:
    """Build an SSE line carrying only a role delta (no content, no finish_reason)."""
    return "data: " + json.dumps({"id": chunk_id, "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}]})


async def _fake_stream(lines: list[str]):
    for line in lines:
        yield line


async def _stream_then_raise(lines: list[str], exc: Exception):
    """Yield the given lines, then raise — mimics an upstream that drops mid-stream."""
    for line in lines:
        yield line
    raise exc


async def _collect(async_gen) -> list[str]:
    return [line async for line in async_gen]


def _parse_finish_sources(sse_lines: list[str]) -> list:
    """Extract the sources list from the finish chunk (second-to-last line before [DONE])."""
    for line in reversed(sse_lines):
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            data = json.loads(line[len("data: ") :])
            extra = data.get("extra")
            if extra and extra != "{}":
                return json.loads(extra).get("sources", [])
    return []


def _collect_content(sse_lines: list[str]) -> str:
    """Concatenate all content deltas from SSE lines."""
    parts = []
    for line in sse_lines:
        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
            continue
        data = json.loads(line[len("data: ") :])
        content = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        if content:
            parts.append(content)
    return "".join(parts)


class TestStreamWithSourceFiltering:
    SOURCES = [{"file": "a.pdf"}, {"file": "b.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_case1_llm_cites_specific_sources(self):
        """Case 1: LLM cites [Sources: 1, 3] → only cited sources returned."""
        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Here is the answer."
        assert _parse_finish_sources(result) == [{"file": "a.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_retrieved_sources_includes_uncited_ones(self):
        """`retrieved_sources` always carries every candidate, unfiltered by citation."""
        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _parse_finish_extra(result)["retrieved_sources"] == self.SOURCES

    @pytest.mark.asyncio
    async def test_content_and_finish_reason_in_same_chunk_keeps_last_token(self):
        """A provider that packs the final token and finish_reason into one chunk
        must not lose that token (regression: `Hello ` + final `world` → `Hello`)."""
        lines = [
            _make_chunk("Hello "),
            _make_content_finish("world"),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Hello world"

    @pytest.mark.asyncio
    async def test_intermediate_content_chunk_never_marked_terminal(self):
        """When the buffer has overflowed and the last token arrives packed with
        finish_reason in one chunk, the mid-stream chunk emitted for it must NOT
        carry finish_reason — else a spec client treats it as terminal, drops the
        delta, and ignores the tail/finish chunks (truncating a long answer)."""
        lines = [
            _make_chunk("A" * 100),  # overflow the buffer (>80) so the next chunk emits mid-stream
            _make_content_finish("B" * 100),  # last token + finish_reason in the same chunk
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        # The whole answer is still delivered.
        assert _collect_content(result) == "A" * 100 + "B" * 100
        # No content-bearing chunk may be marked terminal — only the empty finish chunk.
        for line in result:
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            data = json.loads(line[len("data: ") :])
            choice = data.get("choices", [{}])[0]
            if choice.get("delta", {}).get("content"):
                assert choice.get("finish_reason") is None, f"content chunk marked terminal: {line}"

    @pytest.mark.asyncio
    async def test_upstream_iterator_closed_after_done(self):
        """Breaking on [DONE] must close the upstream generator so the client's
        pooled HTTP connection is released now, not left suspended until GC (an
        `async for` does not close its iterator on break)."""
        closed = {"value": False}

        async def _tracking_stream():
            try:
                yield _make_chunk("Answer.")
                yield _make_finish()
                yield DONE_LINE
                yield _make_chunk("after-done-never-consumed")
            finally:
                closed["value"] = True

        # Hold a reference so a passing assertion can only mean an explicit
        # aclose() ran — not that GC happened to reclaim the generator.
        stream = _tracking_stream()
        result = await _collect(stream_with_source_filtering(stream, self.SOURCES, "test-model"))
        assert result[-1].strip() == "data: [DONE]"
        assert closed["value"] is True, "upstream generator was not closed after [DONE]"

    @pytest.mark.asyncio
    async def test_content_tail_chunk_has_null_finish_reason(self):
        """The content-bearing tail chunk must not carry finish_reason — the
        upstream finish chunk becomes the template, and a spec-compliant client
        treats a finish_reason!=null chunk as terminal and drops its delta."""
        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        tail = None
        for line in result:
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            data = json.loads(line[len("data: ") :])
            choice = data.get("choices", [{}])[0]
            if choice.get("delta", {}).get("content") and data.get("extra"):
                tail = choice
        assert tail is not None, "no content-bearing tail chunk with sources was emitted"
        assert tail["finish_reason"] is None

    @pytest.mark.asyncio
    async def test_case2_llm_says_sources_none(self):
        """Case 2: LLM says [Sources: none] → no sources returned."""
        lines = [
            _make_chunk("I cannot find this in the documents."),
            _make_chunk("\n[Sources: none]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "I cannot find this in the documents."
        assert _parse_finish_sources(result) == []

    @pytest.mark.asyncio
    async def test_case3_llm_no_tag_returns_all_sources(self):
        """Case 3: LLM omits tag entirely → treated as unreported, not uncited; all sources kept."""
        lines = [
            _make_chunk("Answer without any sources tag."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Answer without any sources tag."
        assert _parse_finish_sources(result) == self.SOURCES

    @pytest.mark.asyncio
    async def test_structured_output_preserves_source_like_json_values(self):
        structured = '{"answer":"Use [Source 1]","literal_format":"[Sources: 1]"}'
        lines = [
            _make_chunk(structured),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(
            stream_with_source_filtering(
                _fake_stream(lines),
                self.SOURCES,
                "test-model",
                citation_protocol_active=False,
            )
        )
        assert _collect_content(result) == structured
        assert _parse_finish_sources(result) == self.SOURCES

    @pytest.mark.asyncio
    async def test_direct_output_preserves_terminal_source_marker(self):
        answer = "The requested literal notation is:\n[Sources: 1]"
        lines = [
            _make_chunk(answer),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(
            stream_with_source_filtering(
                _fake_stream(lines),
                [],
                "test-model",
                citation_protocol_active=False,
            )
        )
        assert _collect_content(result) == answer
        assert _parse_finish_sources(result) == []

    @pytest.mark.asyncio
    async def test_multiple_inline_tags_stripped_from_stream(self):
        """Bullet-leak: LLM emits [Sources: X] per bullet. All inline tags must be stripped."""
        lines = [
            _make_chunk("- Claim one about Claude Code.\n"),
            _make_chunk("[Sources: 1]\n"),
            _make_chunk("- Claim two about APEX.\n"),
            _make_chunk("[Sources: 1, 3]\n"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        content = _collect_content(result)
        assert "[Sources:" not in content
        assert "Claim one about Claude Code." in content
        assert "Claim two about APEX." in content
        assert _parse_finish_sources(result) == [{"file": "a.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_context_source_markers_are_stripped_and_rendered_as_sources(self):
        lines = [
            _make_chunk("First claim [Sour"),
            _make_chunk("ce 1]. Second claim [Source 2][Source 3]."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "First claim. Second claim."
        assert _parse_finish_sources(result) == self.SOURCES

    @pytest.mark.asyncio
    async def test_literal_source_marker_is_preserved_without_sources(self):
        lines = [
            _make_chunk("The literal notation [Sour"),
            _make_chunk("ce 1] identifies the first source."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), [], "test-model"))
        assert _collect_content(result) == "The literal notation [Source 1] identifies the first source."
        assert _parse_finish_sources(result) == []

    @pytest.mark.asyncio
    async def test_inline_prose_tag_preserved_in_stream(self):
        """Meta-discussion: a [Sources: 1, 3] inside a sentence must NOT be stripped."""
        lines = [
            _make_chunk("Use the format [Sources: 1, 3]"),
            _make_chunk(" at the very end of your response."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        content = _collect_content(result)
        assert content == "Use the format [Sources: 1, 3] at the very end of your response."
        # No line-terminal tag means the model didn't report citations — kept, not dropped.
        assert _parse_finish_sources(result) == self.SOURCES

    @pytest.mark.asyncio
    async def test_mid_response_tag_stripped_plus_trailing_tag(self):
        """Tag at end of a line mid-response + the final terminal tag both stripped."""
        lines = [
            _make_chunk("Paragraph one ending in a tag. [Sources: 2]\n"),
            _make_chunk("Paragraph two ends the response.\n"),
            _make_chunk("[Sources: 2, 3]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        content = _collect_content(result)
        assert "[Sources:" not in content
        assert "Paragraph one ending in a tag." in content
        assert "Paragraph two ends the response." in content
        assert _parse_finish_sources(result) == [{"file": "b.pdf"}, {"file": "c.pdf"}]


# ---------------------------------------------------------------------------
# Regression for #390 — buffer size scales with number of sources so the
# [Sources: 1, 2, ..., N] tag isn't evicted from the buffer before it can match.
# ---------------------------------------------------------------------------


def test_min_sources_tag_buffer_size_fits_many_sources():
    for n in (1, 10, 60, 100):
        tag = "\n[Sources: " + ", ".join(str(i) for i in range(1, n + 1)) + "]"
        assert _min_sources_tag_buffer_size(n) >= len(tag), n
    assert _min_sources_tag_buffer_size(0) >= 1


class TestStreamClosedWithoutDone:
    """Regression for #715 — upstream closing without `data: [DONE]` must not
    silently drop the answer tail or `extra.sources`."""

    SOURCES = [{"file": "a.pdf"}, {"file": "b.pdf"}, {"file": "c.pdf"}]

    @pytest.mark.asyncio
    async def test_tail_and_sources_still_flushed_without_done(self):
        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
            _make_finish(),
            # No DONE_LINE: upstream connection drops here.
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Here is the answer."
        assert _parse_finish_sources(result) == [{"file": "a.pdf"}, {"file": "c.pdf"}]
        # A [DONE] marker is still emitted downstream so clients don't hang.
        assert result[-1].strip() == "data: [DONE]"

    @pytest.mark.asyncio
    async def test_finish_chunk_flagged_truncated_without_done(self):
        lines = [
            _make_chunk("Partial answer only."),
            _make_finish(),
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        finish_extra = None
        for line in reversed(result):
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                data = json.loads(line[len("data: ") :])
                extra = data.get("extra")
                if extra and extra != "{}":
                    finish_extra = json.loads(extra)
                    break
        assert finish_extra is not None
        assert finish_extra.get("truncated") is True

    @pytest.mark.asyncio
    async def test_clean_done_stream_has_no_truncated_flag(self):
        lines = [
            _make_chunk("Here is the answer."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert "truncated" not in _parse_finish_extra(result)

    @pytest.mark.asyncio
    async def test_preamble_only_close_still_flags_truncated(self):
        # Upstream drops right after the role-preamble chunk, before any content
        # or finish chunk arrives. There is no `chunk_template`, but the preamble
        # must still let us surface `truncated` so the client can tell the stream
        # was cut off rather than a clean empty completion.
        lines = [_make_preamble()]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert result[-1].strip() == "data: [DONE]"
        assert _parse_finish_extra(result).get("truncated") is True

    @pytest.mark.asyncio
    async def test_upstream_raises_after_content_flushes_tail_and_sources(self):
        # A read timeout surfaces as the upstream generator raising mid-stream
        # (not a clean close). The buffered tail + sources must still be flushed
        # and flagged truncated, and no exception should propagate to the caller.
        from core.utils.exceptions import InferenceTimeoutError

        lines = [
            _make_chunk("Here is the answer."),
            _make_chunk("\n[Sources: 1, 3]"),
        ]
        stream = _stream_then_raise(lines, InferenceTimeoutError("LLM request timed out"))
        result = await _collect(stream_with_source_filtering(stream, self.SOURCES, "test-model"))
        assert _collect_content(result) == "Here is the answer."
        assert _parse_finish_sources(result) == [{"file": "a.pdf"}, {"file": "c.pdf"}]
        assert _parse_finish_extra(result).get("truncated") is True
        assert result[-1].strip() == "data: [DONE]"

    @pytest.mark.asyncio
    async def test_upstream_raises_before_any_chunk_reraises(self):
        # Nothing was ever streamed, so there is no partial answer to salvage:
        # the real error must propagate instead of a silent, clean-looking [DONE].
        from core.utils.exceptions import InferenceConnectionError

        stream = _stream_then_raise([], InferenceConnectionError("Cannot reach LLM"))
        with pytest.raises(InferenceConnectionError):
            await _collect(stream_with_source_filtering(stream, self.SOURCES, "test-model"))

    @pytest.mark.asyncio
    async def test_client_disconnect_propagates_without_flush(self):
        # CancelledError (client disconnect) is BaseException, not Exception, so it
        # must propagate untouched rather than be captured as a truncation: no flush,
        # no swallow, letting the router's cancellation handling run.
        stream = _stream_then_raise([_make_chunk("partial")], asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _collect(stream_with_source_filtering(stream, self.SOURCES, "test-model"))

    @pytest.mark.asyncio
    async def test_truncated_answer_is_logged(self):
        # Truncation must surface an explicit, visible "Answer truncated" warning
        # carrying the cause and context (model, delivered length, source count) —
        # and must NOT fire on a clean, [DONE]-terminated stream.
        from loguru import logger

        # Truncated: the stream ends without [DONE].
        records: list[str] = []
        sink_id = logger.add(records.append, level="WARNING", format="{message}")
        try:
            lines = [_make_chunk("Partial answer only."), _make_finish()]
            await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        finally:
            logger.remove(sink_id)
        truncation = [r for r in records if "Answer truncated" in r]
        assert truncation, f"expected a truncation warning, got: {records}"
        assert "model=test-model" in truncation[0]
        assert "reason=connection closed" in truncation[0]

        # Clean [DONE]-terminated stream: no truncation warning.
        records = []
        sink_id = logger.add(records.append, level="WARNING", format="{message}")
        try:
            lines = [_make_chunk("Complete answer."), _make_finish(), DONE_LINE]
            await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        finally:
            logger.remove(sink_id)
        assert not any("Answer truncated" in r for r in records)


def _parse_finish_extra(sse_lines: list[str]) -> dict:
    for line in reversed(sse_lines):
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            data = json.loads(line[len("data: ") :])
            extra = data.get("extra")
            if extra and extra != "{}":
                return json.loads(extra)
    return {}


class TestStreamWithManySources:
    @pytest.mark.asyncio
    async def test_60_source_citation_survives_buffer_eviction(self):
        sources = [{"file": f"src-{i}.pdf"} for i in range(60)]
        body = "This is the answer body. " * 30
        cited = ", ".join(str(i) for i in range(1, 61))
        tag = f"\n[Sources: {cited}]"
        lines = [_make_chunk(p) for p in [body[i : i + 5] for i in range(0, len(body), 5)]]
        lines.append(_make_chunk(tag))
        lines.append(_make_finish())
        lines.append(DONE_LINE)

        result = await _collect(stream_with_source_filtering(_fake_stream(lines), sources, "test-model"))

        content = _collect_content(result)
        assert "[Sources:" not in content
        assert content.startswith("This is the answer body.")
        assert content.endswith("This is the answer body.")
        assert _parse_finish_sources(result) == sources
