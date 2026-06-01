"""Tests for the map-reduce prompt builder."""

from __future__ import annotations

from core.prompts.map_reduce_builder import (
    SYSTEM_PROMPT_MAP,
    USER_PROMPT_TEMPLATE,
    build_map_messages,
)


def test_build_map_messages_returns_system_then_user_with_substitution():
    msgs = build_map_messages(query="what is rag?", content="some doc body")
    assert len(msgs) == 2
    assert msgs[0] == {"role": "system", "content": SYSTEM_PROMPT_MAP}
    assert msgs[1]["role"] == "user"
    user_text = msgs[1]["content"]
    assert "what is rag?" in user_text
    assert "some doc body" in user_text


def test_user_prompt_template_uses_named_placeholders():
    """Lock the template's named placeholders so accidental positional refactors
    don't silently change the wire format."""
    rendered = USER_PROMPT_TEMPLATE.format(query="Q", content="C")
    assert "Q" in rendered
    assert "C" in rendered
