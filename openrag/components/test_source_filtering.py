"""Tests for source citation extraction and filtering utilities."""

import json

import pytest
from components.utils import (
    extract_and_strip_sources_block,
    filter_sources_by_citations,
    format_sources_as_markdown,
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


class TestFormatSourcesAsMarkdown:
    """Behavior of the markdown source block injected into content when
    INLINE_SOURCES_IN_CONTENT=true.
    """

    SOURCES = [
        {"filename": "a.pdf", "title": "Doc A", "file_url": "https://x/a", "relevance_score": 0.9},
        {"filename": "b.pdf", "title": "Doc B", "file_url": "https://x/b", "relevance_score": 0.7},
        {"filename": "a.pdf", "title": "Doc A", "file_url": "https://x/a", "relevance_score": 0.5},
    ]

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("INLINE_SOURCES_IN_CONTENT", raising=False)
        assert format_sources_as_markdown(self.SOURCES) == ""

    def test_disabled_explicit(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "false")
        assert format_sources_as_markdown(self.SOURCES) == ""

    def test_enabled_basic(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        result = format_sources_as_markdown(self.SOURCES)
        assert "**Sources :**" in result
        # Dedup on file_url: a.pdf appears twice in input, once in output (best score)
        assert result.count("Doc A") == 1
        assert result.count("Doc B") == 1
        # Best score wins
        assert "score 0.90" in result
        assert "score 0.50" not in result
        # Ranked: Doc A (0.9) before Doc B (0.7)
        assert result.find("Doc A") < result.find("Doc B")
        # URL is rendered as markdown link (angle-bracket form)
        assert "[Doc A](<https://x/a>)" in result

    def test_empty_sources(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        assert format_sources_as_markdown([]) == ""

    def test_min_score_filter(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        monkeypatch.setenv("INLINE_SOURCES_MIN_SCORE", "0.8")
        result = format_sources_as_markdown(self.SOURCES)
        assert "Doc A" in result  # score 0.9 ≥ 0.8
        assert "Doc B" not in result  # score 0.7 < 0.8

    def test_top_k_limit(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        monkeypatch.setenv("INLINE_SOURCES_TOP_K", "1")
        result = format_sources_as_markdown(self.SOURCES)
        assert "Doc A" in result
        assert "Doc B" not in result  # capped at 1

    def test_web_sources_included(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        sources = [
            {
                "source_type": "web",
                "url": "https://example.com/page",
                "title": "Web Result",
                "snippet": "...",
                "relevance_score": 0.8,
            },
            {"source_type": "document", "file_url": "https://x/doc.pdf", "title": "Doc", "relevance_score": 0.7},
        ]
        result = format_sources_as_markdown(sources)
        assert "Web Result" in result
        assert "[Web Result](<https://example.com/page>)" in result
        assert "Doc" in result

    def test_label_special_chars_escaped(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        sources = [{"file_url": "https://x/a", "title": "Report [draft] Q1|Q2", "relevance_score": 0.9}]
        result = format_sources_as_markdown(sources)
        assert "[Report \\[draft\\] Q1\\|Q2](<https://x/a>)" in result

    def test_url_with_parens_and_spaces_uses_angle_brackets(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        sources = [
            {
                "file_url": "https://en.wikipedia.org/wiki/Foo_(bar) baz",
                "title": "Wiki",
                "relevance_score": 0.9,
            }
        ]
        result = format_sources_as_markdown(sources)
        # Bare-URL form would break on "(", ")", and " ". Angle-bracket form
        # tolerates all three, so the destination must be wrapped.
        assert "[Wiki](<https://en.wikipedia.org/wiki/Foo_(bar) baz>)" in result

    def test_url_with_angle_brackets_percent_encoded(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        sources = [{"file_url": "https://x/a<b>c", "title": "T", "relevance_score": 0.9}]
        result = format_sources_as_markdown(sources)
        # "<" / ">" close the angle-bracket destination prematurely; encode them.
        assert "[T](<https://x/a%3Cb%3Ec>)" in result


class TestStreamWithInlineSources:
    """When INLINE_SOURCES_IN_CONTENT=true, the stream emits an extra delta
    chunk carrying the markdown source block before the finish_reason chunk.
    Pre-existing tests verify the off-default behavior remains intact.
    """

    SOURCES = [
        {"filename": "a.pdf", "title": "Doc A", "file_url": "https://x/a", "relevance_score": 0.9},
    ]

    @pytest.mark.asyncio
    async def test_inline_sources_appended_to_stream(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "true")
        lines = [
            _make_chunk("The answer."),
            _make_chunk("\n[Sources: 1]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        # Concatenated content should include the original answer + the
        # markdown source block (no [Sources: 1] tag — stripped as before).
        full_content = _collect_content(result)
        assert "The answer." in full_content
        assert "**Sources :**" in full_content
        assert "[Doc A](<https://x/a>)" in full_content
        assert "[Sources: 1]" not in full_content
        # The structured `extra` field still reaches the finish chunk.
        assert _parse_finish_sources(result) == self.SOURCES

    @pytest.mark.asyncio
    async def test_no_inline_when_disabled(self, monkeypatch):
        monkeypatch.setenv("INLINE_SOURCES_IN_CONTENT", "false")
        lines = [
            _make_chunk("The answer."),
            _make_chunk("\n[Sources: 1]"),
            _make_finish(),
            DONE_LINE,
        ]
        result = await _collect(stream_with_source_filtering(_fake_stream(lines), self.SOURCES, "test-model"))
        full_content = _collect_content(result)
        assert full_content == "The answer."
        assert "**Sources :**" not in full_content
