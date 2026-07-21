"""Tests for source citation extraction and filtering utilities."""

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


class TestFilterSourcesByCitations:
    def test_basic_filtering(self):
        sources = ["a", "b", "c", "d", "e"]
        result = filter_sources_by_citations(sources, {1, 3, 5})
        assert result == ["a", "c", "e"]

    def test_none_citations_returns_all(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, None)
        assert result == ["a", "b", "c"]

    def test_empty_citations_returns_empty(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, set())
        assert result == []

    def test_out_of_range_citations_fallback(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, {99})
        assert result == ["a", "b", "c"]

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


DONE_LINE = "data: [DONE]"


async def _fake_stream(lines: list[str]):
    for line in lines:
        yield line


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
    async def test_case3_llm_no_tag_fallback_all(self):
        """Case 3: LLM omits tag entirely → fallback to all sources."""
        lines = [
            _make_chunk("Answer without any sources tag."),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        assert _collect_content(result) == "Answer without any sources tag."
        assert _parse_finish_sources(result) == self.SOURCES

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
        # No line-terminal tag → fallback to all sources
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
