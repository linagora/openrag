"""Progress in an answer's stream: what the client is shown before and between the answer's text.

A thinking model streams its reasoning as ``delta.reasoning_content`` (vLLM,
DeepSeek, Moonshot) or ``delta.reasoning`` (OpenRouter). In ``agentic`` the
agent adds an ``extra.agent_step`` event when a tool call starts and another
when it ends. Neither is answer text: they reach the client as they come, the
citation filter leaves them alone, and the empty-answer retry does not hold
them back, so a client can show the model at work while the answer is still
to come.
"""

from __future__ import annotations

#: The key of an agent step event in a chunk's ``extra``.
AGENT_STEP = "agent_step"

#: Where providers put a reasoning delta.
REASONING_KEYS = ("reasoning_content", "reasoning")


def reasoning_text(delta: dict) -> str:
    """The reasoning text of a chunk's *delta*, ``""`` for none."""
    for key in REASONING_KEYS:
        text = delta.get(key)
        if isinstance(text, str) and text:
            return text
    return ""


def is_progress(chunk: dict) -> bool:
    """Whether *chunk* shows the model at work and holds no answer: thinking or an agent step.

    A chunk with answer text, a tool call or a finish reason is not progress,
    even when it also carries reasoning.
    """
    choices = chunk.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    if delta.get("content") or delta.get("tool_calls") or choice.get("finish_reason"):
        return False
    extra = chunk.get("extra")
    return bool(reasoning_text(delta)) or (isinstance(extra, dict) and AGENT_STEP in extra)


__all__ = ["AGENT_STEP", "REASONING_KEYS", "is_progress", "reasoning_text"]
