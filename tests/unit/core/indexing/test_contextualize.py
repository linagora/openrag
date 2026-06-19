from __future__ import annotations

import pytest
from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk


class DictLLM:
    async def chat(self, messages, **kwargs):
        return {"choices": [{"message": {"content": "document-level context"}}]}


@pytest.mark.asyncio
async def test_contextualizer_accepts_openai_style_chat_response():
    contextualizer = ChunkContextualizer(DictLLM(), "System prompt")

    result = await contextualizer.contextualize([Chunk(id="c1", text="chunk body", partition="p")])

    assert result[0].context == "document-level context"
    assert result[0].content == "chunk body"


class _GatingLLM:
    """LLM stub that records whether the LLM gate was held during ``chat``."""

    def __init__(self, gate_held: list[bool]):
        self._gate_held = gate_held

    async def chat(self, messages, **kwargs):
        self._gate_held.append(_TrackingGate.depth > 0)
        return {"choices": [{"message": {"content": "ctx"}}]}


class _TrackingGate:
    """Async context manager standing in for the distributed LLM semaphore."""

    depth = 0

    async def __aenter__(self):
        type(self).depth += 1
        return self

    async def __aexit__(self, *exc):
        type(self).depth -= 1


@pytest.mark.asyncio
async def test_contextualizer_holds_llm_semaphore_around_chat():
    _TrackingGate.depth = 0
    gate_held: list[bool] = []
    contextualizer = ChunkContextualizer(_GatingLLM(gate_held), "System prompt", llm_semaphore=_TrackingGate())

    await contextualizer.contextualize([Chunk(id="c1", text="chunk body", partition="p")])

    # The injected gate was entered for the chat call and released afterwards.
    assert gate_held == [True]
    assert _TrackingGate.depth == 0
