"""Unit tests for ``core.utils.stream_progress``: telling progress lines from the answer's."""

from __future__ import annotations

import pytest
from core.utils.stream_progress import is_progress, reasoning_text


def _chunk(finish_reason=None, extra=None, **delta) -> dict:
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
    if extra is not None:
        chunk["extra"] = extra
    return chunk


@pytest.mark.parametrize(
    ("chunk", "progress"),
    [
        (_chunk(reasoning_content="Let me think"), True),
        (_chunk(reasoning="Let me think"), True),  # OpenRouter's key
        (_chunk(reasoning_content="The", content=""), True),  # kimi sends an empty content beside it
        (_chunk(extra={"agent_step": {"type": "tool_call"}}), True),
        (_chunk(reasoning_content="x", content="Three"), False),  # answer text
        (_chunk(reasoning_content="x", tool_calls=[{"index": 0}]), False),
        (_chunk("stop", reasoning_content="x"), False),
        (_chunk(role="assistant"), False),
        (_chunk(extra={"sources": []}), False),
        ({"choices": [], "usage": {}}, False),
    ],
)
def test_progress_is_thinking_or_an_agent_step_with_no_answer(chunk, progress):
    assert is_progress(chunk) is progress


def test_reasoning_text_reads_either_key():
    assert reasoning_text({"reasoning_content": "a"}) == "a"
    assert reasoning_text({"reasoning": "b"}) == "b"
    assert reasoning_text({"reasoning_content": "", "content": "c"}) == ""
