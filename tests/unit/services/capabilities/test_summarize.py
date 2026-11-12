"""Map-reduce: the chunks relevant to the query, kept as their summaries."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from core.models.chunk import Chunk
from services.capabilities.summarize import Summarizer


class FakeLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return {"choices": [{"message": {"content": self._responses.pop(0)}}]}


@asynccontextmanager
async def _no_semaphore():
    yield


def _summarizer(llm) -> Summarizer:
    config = SimpleNamespace(
        map_reduce=SimpleNamespace(initial_batch_size=2, expansion_batch_size=2, max_total_documents=4)
    )
    return Summarizer(llm=llm, config=config, semaphore=_no_semaphore)


def _docs(*ids: str) -> list:
    return [Chunk(id=i, text=i.upper(), metadata={"_id": i}).to_langchain() for i in ids]


RELEVANT = json.dumps({"relevancy": True, "summary": "kept"})
IRRELEVANT = json.dumps({"relevancy": False, "summary": ""})


@pytest.mark.asyncio
async def test_map_reduce_keeps_relevant_drops_irrelevant():
    out = await _summarizer(FakeLLM([RELEVANT, IRRELEVANT])).summarize("q", _docs("a", "b"))
    assert len(out) == 1
    assert out[0].page_content == "kept"
    assert out[0].metadata["_id"] == "a"


@pytest.mark.asyncio
async def test_a_batch_with_nothing_relevant_stops_the_run():
    llm = FakeLLM([IRRELEVANT, IRRELEVANT])
    assert await _summarizer(llm).summarize("q", _docs("a", "b", "c", "d")) == []
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_an_unparsable_verdict_drops_the_chunk():
    out = await _summarizer(FakeLLM(["not json", RELEVANT])).summarize("q", _docs("a", "b"))
    assert [doc.metadata["_id"] for doc in out] == ["b"]
