from __future__ import annotations

import pytest
from core.indexing.contextualize import ChunkContextualizer
from core.models.chunk import Chunk, ChunkType


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


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return {"choices": [{"message": {"content": "ordinary context"}}]}


@pytest.mark.asyncio
async def test_contextualizer_leaves_structured_table_rows_and_legends_unchanged():
    llm = _RecordingLLM()
    contextualizer = ChunkContextualizer(llm, "System prompt")
    row = Chunk(
        id="row",
        text="deterministic row text",
        chunk_type=ChunkType.TABLE,
        metadata={"table_content_kind": "row", "table_id": "table-a"},
    )
    legend = Chunk(
        id="legend",
        text="deterministic legend text",
        chunk_type=ChunkType.TABLE,
        metadata={"table_content_kind": "legend", "table_id": "table-a"},
    )

    result = await contextualizer.contextualize([row, legend], filename="table.pdf")

    assert llm.calls == []
    assert result[0] is row
    assert result[1] is legend
    assert result[0].model_dump() == row.model_dump()
    assert result[1].model_dump() == legend.model_dump()


@pytest.mark.asyncio
async def test_contextualizer_still_processes_ordinary_chunks_in_a_mixed_batch():
    llm = _RecordingLLM()
    contextualizer = ChunkContextualizer(llm, "System prompt")
    table_row = Chunk(
        id="row",
        text="deterministic row text",
        chunk_type=ChunkType.TABLE,
        metadata={"table_content_kind": "row", "table_id": "table-a"},
    )
    ordinary = Chunk(id="text", text="ordinary paragraph", chunk_type=ChunkType.TEXT)

    result = await contextualizer.contextualize([table_row, ordinary], filename="mixed.pdf")

    assert len(llm.calls) == 1
    assert result[0] is table_row
    assert result[0].text == "deterministic row text"
    assert result[0].context is None
    assert result[0].content is None
    assert result[1].context == "ordinary context"
    assert result[1].content == "ordinary paragraph"
    assert "[CONTEXT]" in result[1].text
    assert "ordinary paragraph" in result[1].text
