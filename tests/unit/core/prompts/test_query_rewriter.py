"""Tests for the HyDe / multi-query prompt builders."""

from __future__ import annotations

from core.prompts.query_rewriter import (
    MULTI_QUERY_SEPARATOR,
    build_hyde_prompt,
    build_multi_query_prompt,
    split_multi_query_response,
)


def test_build_hyde_prompt_substitutes_question():
    out = build_hyde_prompt("Q: {question}", "what is rag?")
    assert out == "Q: what is rag?"


def test_build_multi_query_prompt_substitutes_query_and_k():
    out = build_multi_query_prompt("Generate {k_queries} variants of: {query}", "what is rag?", 5)
    assert "5" in out
    assert "what is rag?" in out


def test_split_multi_query_response_splits_on_separator_and_trims():
    raw = f" first query {MULTI_QUERY_SEPARATOR} second query {MULTI_QUERY_SEPARATOR}third"
    assert split_multi_query_response(raw) == ["first query", "second query", "third"]


def test_split_multi_query_response_drops_empty_entries():
    raw = f"   {MULTI_QUERY_SEPARATOR}only{MULTI_QUERY_SEPARATOR}{MULTI_QUERY_SEPARATOR}  "
    assert split_multi_query_response(raw) == ["only"]


def test_split_multi_query_response_empty_string_returns_empty_list():
    assert split_multi_query_response("") == []


def test_split_multi_query_response_accepts_custom_separator():
    raw = "a||b||c"
    assert split_multi_query_response(raw, separator="||") == ["a", "b", "c"]
