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
