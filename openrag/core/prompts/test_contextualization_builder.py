"""Tests for the chunk-contextualization prompt builder."""

from __future__ import annotations

from openrag.core.prompts.contextualization_builder import (
    BASE_CHUNK_FORMAT,
    CHUNK_FORMAT,
    build_messages,
    build_user_message,
    wrap_chunk_with_context,
)


def test_build_user_message_includes_filename_and_lang():
    out = build_user_message(
        filename="doc.pdf",
        first_chunks_text=["intro chunk A", "intro chunk B"],
        prev_chunks_text=["prev chunk"],
        current_chunk_text="here is the current chunk",
        lang="fr",
    )
    assert "doc.pdf" in out
    assert "intro chunk A" in out
    assert "intro chunk B" in out
    assert "prev chunk" in out
    assert "here is the current chunk" in out
    assert "fr language" in out


def test_build_user_message_handles_empty_history():
    out = build_user_message(
        filename="doc.pdf",
        first_chunks_text=[],
        prev_chunks_text=[],
        current_chunk_text="solo chunk",
    )
    assert "solo chunk" in out
    assert "en language" in out  # default lang


def test_build_messages_returns_system_then_user():
    msgs = build_messages(
        system_prompt="SYS",
        filename="doc.pdf",
        first_chunks_text=["a"],
        prev_chunks_text=["b"],
        current_chunk_text="c",
    )
    assert len(msgs) == 2
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["role"] == "user"
    assert "doc.pdf" in msgs[1]["content"]


def test_wrap_chunk_with_context_uses_full_format_when_context_given():
    out = wrap_chunk_with_context(content="body", filename="f.pdf", chunk_context="ctx")
    assert "[CONTEXT]" in out
    assert "ctx" in out
    assert "[CHUNK_START]" in out
    assert "body" in out
    assert "[CHUNK_END]" in out
    # Sanity: result uses the documented CHUNK_FORMAT template.
    assert out == CHUNK_FORMAT.format(content="body", chunk_context="ctx", filename="f.pdf")


def test_wrap_chunk_with_context_uses_base_format_when_context_empty():
    out = wrap_chunk_with_context(content="body", filename="f.pdf", chunk_context="")
    assert "[CONTEXT]" not in out
    assert "[CHUNK_START]" in out
    assert "body" in out
    assert out == BASE_CHUNK_FORMAT.format(content="body", filename="f.pdf")


def test_wrap_chunk_with_context_defaults_chunk_context_to_empty():
    out = wrap_chunk_with_context(content="body", filename="f.pdf")
    assert "[CONTEXT]" not in out


def test_wrap_chunk_with_context_treats_whitespace_only_as_empty():
    out = wrap_chunk_with_context(content="body", filename="f.pdf", chunk_context="   \n\t")
    assert "[CONTEXT]" not in out
    assert out == BASE_CHUNK_FORMAT.format(content="body", filename="f.pdf")
