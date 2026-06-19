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
