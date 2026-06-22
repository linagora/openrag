import asyncio

import pytest
from core.indexing.topic_tags import TopicTagger
from core.models.chunk import Chunk


class FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **kwargs):
        self.messages.append(messages)
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.mark.asyncio
async def test_topic_tagger_extracts_normalized_unique_tags():
    llm = FakeLLM('["Finance", "climate risk", "Finance", ""]')
    tagger = TopicTagger(llm, "extract topics")

    tags = await tagger.tag(
        [Chunk(id="c1", text="green finance"), Chunk(id="c2", text="portfolio risk")],
        filename="report.pdf",
        max_tags=3,
        lang="en",
    )

    assert tags == ["Finance", "climate risk"]
    assert "report.pdf" in llm.messages[0][1]["content"]
    assert "green finance" in llm.messages[0][1]["content"]


@pytest.mark.asyncio
async def test_topic_tagger_falls_back_to_empty_list_on_bad_response():
    llm = FakeLLM("this is not structured")
    tagger = TopicTagger(llm, "extract topics")

    assert await tagger.tag([Chunk(id="c1", text="hello")], max_tags=5) == []


@pytest.mark.asyncio
async def test_topic_tagger_returns_empty_when_max_tags_is_not_positive():
    llm = FakeLLM('["finance"]')
    tagger = TopicTagger(llm, "extract topics")

    assert await tagger.tag([Chunk(id="c1", text="hello")], max_tags=0) == []
    assert llm.messages == []


@pytest.mark.asyncio
async def test_topic_tagger_timeout_zero_uses_timeout_path():
    class SlowLLM:
        async def chat(self, messages: list[dict[str, str]], **kwargs):
            await asyncio.sleep(0)
            return {"choices": [{"message": {"content": '["finance"]'}}]}

    tagger = TopicTagger(SlowLLM(), "extract topics", timeout_seconds=0)

    assert await tagger.tag([Chunk(id="c1", text="hello")], max_tags=5) == []


@pytest.mark.asyncio
async def test_topic_tagger_parses_json_array_before_bracket_suffix():
    llm = FakeLLM('["finance"] [Sources: 1]')
    tagger = TopicTagger(llm, "extract topics")

    assert await tagger.tag([Chunk(id="c1", text="hello")], max_tags=5) == ["finance"]
