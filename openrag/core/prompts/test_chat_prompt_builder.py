"""Tests for chat_prompt_builder — these lock in the exact wire format."""

from __future__ import annotations

from openrag.core.prompts.chat_prompt_builder import (
    EMPTY_CONTEXT_MESSAGE,
    SOURCE_SEPARATOR,
    format_context,
    format_web_context,
    prepend_system_prompt,
)


def _word_tokens(text: str) -> int:
    """Simple deterministic token counter: 1 token per whitespace-delimited word."""
    return len(text.split())


def test_format_context_empty_returns_placeholder():
    text, included = format_context([], max_context_tokens=100, length_function=_word_tokens)
    assert text == EMPTY_CONTEXT_MESSAGE
    assert included == []


def test_format_context_numbers_sources_and_separates():
    docs = ["alpha beta", "gamma delta epsilon"]
    text, included = format_context(docs, max_context_tokens=100, length_function=_word_tokens)
    assert "[Source 1]\nalpha beta" in text
    assert "[Source 2]\ngamma delta epsilon" in text
    assert SOURCE_SEPARATOR in text
    assert included == [0, 1]


def test_format_context_drops_to_fit_budget():
    docs = ["one two", "three four five", "six"]
    # _word_tokens("[Source 1]\n") = 1, doc1 = 2, prefix2 = 1, doc2 = 3 -> total 7
    text, included = format_context(docs, max_context_tokens=4, length_function=_word_tokens)
    assert "[Source 1]" in text
    assert "[Source 2]" not in text
    assert included == [0]


def test_format_context_no_numbering():
    docs = ["a", "b"]
    text, included = format_context(docs, max_context_tokens=100, length_function=_word_tokens, number_sources=False)
    assert "[Source" not in text
    assert text == f"a{SOURCE_SEPARATOR}b"
    assert included == [0, 1]


class _FakeWeb:
    def __init__(self, title: str, url: str, snippet: str, content: str | None = None):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.content = content


def test_format_web_context_uses_content_when_present():
    results = [_FakeWeb("T1", "u1", "snip1", content="full body")]
    text, nums, _ = format_web_context(results, length_function=_word_tokens, max_tokens=100)
    assert "full body" in text
    assert "snip1" not in text
    assert nums == [1]


def test_format_web_context_falls_back_to_snippet():
    results = [_FakeWeb("T1", "u1", "snip1", content=None)]
    text, _, _ = format_web_context(results, length_function=_word_tokens, max_tokens=100)
    assert "snip1" in text


def test_format_web_context_continues_numbering_with_start_index():
    results = [_FakeWeb("T1", "u1", "snip1")]
    text, nums, _ = format_web_context(results, length_function=_word_tokens, start_index=4, max_tokens=100)
    assert "[Source 4]" in text
    assert nums == [4]


def test_prepend_system_prompt_does_not_mutate_input():
    msgs = [{"role": "user", "content": "hi"}]
    out = prepend_system_prompt(
        msgs,
        system_template="ctx={context} date={current_date}",
        context="C",
        current_date="2026-04-29",
    )
    assert msgs == [{"role": "user", "content": "hi"}]
    assert out[0] == {"role": "system", "content": "ctx=C date=2026-04-29"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_format_web_context_empty_returns_empty_tuple():
    text, nums, total = format_web_context([], length_function=_word_tokens)
    assert text == ""
    assert nums == []
    assert total == 0


def test_format_web_context_drops_overflow_block_after_first_fits():
    """If a later block would push past max_tokens we break — but only after
    at least one block has been admitted (parts truthy guard)."""
    results = [
        _FakeWeb("T1", "u1", "short body"),
        _FakeWeb("T2", "u2", "this snippet has many many many many many words that will overflow"),
    ]
    text, nums, _ = format_web_context(results, length_function=_word_tokens, max_tokens=10)
    assert "[Source 1]" in text
    assert "[Source 2]" not in text
    assert nums == [1]
