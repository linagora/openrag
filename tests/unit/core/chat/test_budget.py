"""Fitting the sources into what the answering model's context window leaves."""

from types import SimpleNamespace

import pytest
from core.chat.budget import fit_prompt, fit_sources
from core.utils.exceptions import ContextWindowExceededError


def _doc(text: str) -> SimpleNamespace:
    return SimpleNamespace(page_content=text, metadata={})


def _words(text: str) -> int:
    return len(text.split())


# --------------------------------------------------------------------------- #
# fit_sources
# --------------------------------------------------------------------------- #


def test_fit_sources_caps_at_top_n():
    docs = [_doc("a"), _doc("b"), _doc("c")]
    assert fit_sources(docs, top_n=2, headroom=None, length_function=_words) == [0, 1]


def test_fit_sources_skips_a_block_too_big_for_the_headroom():
    """A long document is skipped, not the end of the list: a shorter one after it may still fit."""
    docs = [_doc("one two three four five six"), _doc("short")]
    # "[Source 1]\nshort" is 3 words; the long block alone is 8.
    assert fit_sources(docs, top_n=5, headroom=4, length_function=_words) == [1]


# --------------------------------------------------------------------------- #
# fit_prompt
# --------------------------------------------------------------------------- #


def _assemble(max_web: int = 99):
    """Renders 'D' per document and 'W' per web result; at most *max_web* web results fit."""

    def assemble(docs: list, web: list) -> tuple[str, list]:
        shown = web[:max_web]
        return " ".join(["D"] * len(docs) + ["W"] * len(shown)), shown

    return assemble


def test_fit_prompt_drops_nothing_when_the_window_is_unknown():
    rendered, docs, web = fit_prompt(_assemble(), [1, 2, 3], ["w"], measure=_words, max_tokens=None)
    assert (rendered, docs, web) == ("D D D W", [1, 2, 3], ["w"])


def test_fit_prompt_drops_documents_first_from_the_end():
    rendered, docs, web = fit_prompt(_assemble(), [1, 2, 3], ["w1", "w2"], measure=_words, max_tokens=3)
    assert (rendered, docs, web) == ("D W W", [1], ["w1", "w2"])


def test_fit_prompt_keeps_every_web_candidate_while_documents_shrink():
    """Dropping a document can make room for a web result that didn't fit before."""
    calls = []

    def assemble(docs: list, web: list) -> tuple[str, list]:
        calls.append((list(docs), list(web)))
        shown = web[: 3 - len(docs)]
        return " ".join(["D"] * len(docs) + ["W"] * len(shown)), shown

    _, docs, web = fit_prompt(assemble, [1, 2, 3], ["w1", "w2"], measure=_words, max_tokens=2)

    assert all(candidates == ["w1", "w2"] for _, candidates in calls)
    assert (docs, web) == ([], ["w1", "w2"])


def test_fit_prompt_drops_web_results_shown_once_no_document_is_left():
    rendered, docs, web = fit_prompt(_assemble(), [1], ["w1", "w2", "w3"], measure=_words, max_tokens=1)
    assert (rendered, docs, web) == ("W", [], ["w1"])


def test_fit_prompt_raises_when_even_no_source_fits():
    def assemble(docs: list, web: list) -> tuple[str, list]:
        return "instructions " + " ".join(["D"] * len(docs)), []

    with pytest.raises(ContextWindowExceededError):
        fit_prompt(assemble, [1, 2], [], measure=_words, max_tokens=0)
