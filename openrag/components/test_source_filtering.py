"""Tests for source citation extraction and filtering utilities."""

from components.utils import extract_and_strip_sources_block, filter_sources_by_citations


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
        assert citations == set()

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
        assert citations == set()

    def test_sources_mid_text_not_stripped(self):
        text = "Answer [Sources: 1, 2] and more text after"
        clean, citations = extract_and_strip_sources_block(text)
        assert clean == text
        assert citations == set()

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


class TestFilterSourcesByCitations:
    def test_basic_filtering(self):
        sources = ["a", "b", "c", "d", "e"]
        result = filter_sources_by_citations(sources, {1, 3, 5})
        assert result == ["a", "c", "e"]

    def test_empty_citations_returns_all(self):
        sources = ["a", "b", "c"]
        result = filter_sources_by_citations(sources, set())
        assert result == ["a", "b", "c"]

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
