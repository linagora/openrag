"""Retriever strategy tests with fake searcher + LLM.

These exercise the strategy logic without Ray, OpenAI, or LangChain — proving
the new core/ retriever has clean dependencies.
"""

from __future__ import annotations

import pytest
from core.llm.llm import chat_content
from core.models.chunk import Chunk
from core.retrieval.retriever import (
    HyDeRetriever,
    MultiQueryRetriever,
    SingleRetriever,
    retriever_registry,
)
from core.retrieval.searcher import RetrievalSearcher


class FakeSearcher(RetrievalSearcher):
    """Records calls; returns canned chunks."""

    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.multi_calls: list[dict] = []
        self.related_calls: list[dict] = []
        self.ancestor_calls: list[dict] = []
        self.search_result: list[Chunk] = []
        self.multi_result: list[Chunk] = []
        self.related_result: list[Chunk] = []
        self.ancestor_result: list[Chunk] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.search_result)

    async def multi_query_search(self, **kwargs):
        self.multi_calls.append(kwargs)
        return list(self.multi_result)

    async def get_related_chunks(self, **kwargs):
        self.related_calls.append(kwargs)
        return list(self.related_result)

    async def get_ancestor_chunks(self, **kwargs):
        self.ancestor_calls.append(kwargs)
        return list(self.ancestor_result)


class FakeLLM:
    """Test double for the ``LLM`` port.

    Returns the OpenAI-shaped ``dict`` the real port declares (``LLM.chat ->
    dict``), NOT a bare ``str``. A previous ``-> str`` fake was more permissive
    than production and hid #703: ``multiQuery``/``hyde`` called ``.split()`` /
    ``.strip()`` straight on the response and raised ``AttributeError`` on every
    real query while these tests stayed green.
    """

    def __init__(self, response: str) -> None:
        self.response = response
        self.chat_calls: list[list[dict]] = []

    @staticmethod
    def _envelope(content: str) -> dict:
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    async def generate(self, prompt: str, **kwargs) -> dict:
        return self._envelope(self.response)

    async def chat(self, messages: list[dict], **kwargs) -> dict:
        self.chat_calls.append(messages)
        return self._envelope(self.response)


def _chunk(idv: str, text: str = "x", document_id: str = "", partition: str = "p1") -> Chunk:
    return Chunk(id=idv, text=text, document_id=document_id, partition=partition)


def test_registry_has_three_strategies():
    assert set(retriever_registry.list_registered()) == {"single", "multiQuery", "hyde"}


@pytest.mark.asyncio
async def test_single_retriever_passes_through_to_searcher():
    s = FakeSearcher()
    s.search_result = [_chunk("1"), _chunk("2")]
    r = SingleRetriever(searcher=s, top_k=4, similarity_threshold=0.3, with_surrounding_chunks=False)
    out = await r.retrieve(partition=["p1"], query="hello", filter="x>0", filter_params={"a": 1})
    assert [c.id for c in out] == ["1", "2"]
    assert s.search_calls == [
        {
            "query": "hello",
            "partition": ["p1"],
            "top_k": 4,
            "filter": "x>0",
            "filter_params": {"a": 1},
            "similarity_threshold": 0.3,
            "with_surrounding_chunks": False,
        }
    ]


@pytest.mark.asyncio
async def test_multi_query_retriever_splits_llm_response():
    s = FakeSearcher()
    s.multi_result = [_chunk("a")]
    llm = FakeLLM(response="Q one[SEP]Q two[SEP]Q three")
    r = MultiQueryRetriever(
        searcher=s,
        llm=llm,
        multi_query_template="generate {k_queries} variants of: {query}",
        k_queries=3,
        top_k=5,
    )
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["Q one", "Q two", "Q three"]
    assert s.multi_calls[0]["top_k_per_query"] == 5


@pytest.mark.asyncio
async def test_multi_query_falls_back_to_seed_on_empty_response():
    s = FakeSearcher()
    llm = FakeLLM(response="")
    r = MultiQueryRetriever(
        searcher=s,
        llm=llm,
        multi_query_template="{query} {k_queries}",
        k_queries=3,
    )
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["seed"]


@pytest.mark.asyncio
async def test_hyde_retriever_uses_hyde_only_by_default():
    s = FakeSearcher()
    llm = FakeLLM(response="A hypothetical answer paragraph.")
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="Answer: {question}")
    await r.retrieve(partition=["p1"], query="real question")
    assert s.multi_calls[0]["queries"] == ["A hypothetical answer paragraph."]


@pytest.mark.asyncio
async def test_hyde_retriever_combine_appends_original_query():
    s = FakeSearcher()
    llm = FakeLLM(response="hypothetical")
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="Answer: {question}", combine=True)
    await r.retrieve(partition=["p1"], query="real")
    assert s.multi_calls[0]["queries"] == ["hypothetical", "real"]


@pytest.mark.asyncio
async def test_expansion_disabled_returns_unchanged():
    s = FakeSearcher()
    r = SingleRetriever(searcher=s)
    initial = [_chunk("1")]
    out = await r.expand_search_results(initial)
    assert out is initial
    assert not s.related_calls
    assert not s.ancestor_calls


@pytest.mark.asyncio
async def test_expansion_with_related_dedupes_by_id():
    s = FakeSearcher()
    s.related_result = [_chunk("1"), _chunk("3")]  # "1" already in results
    r = SingleRetriever(searcher=s, include_related=True)
    initial = [
        Chunk(id="1", text="x", partition="p1", metadata={"relationship_id": "r1"}),
    ]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1", "3"]


@pytest.mark.asyncio
async def test_expansion_with_ancestors_calls_searcher():
    s = FakeSearcher()
    s.ancestor_result = [_chunk("99", document_id="f1")]
    r = SingleRetriever(searcher=s, include_ancestors=True, related_limit=20, max_ancestor_depth=2)
    initial = [Chunk(id="1", text="x", partition="p1", document_id="f1")]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1", "99"]
    assert s.ancestor_calls[0]["partition"] == "p1"
    assert s.ancestor_calls[0]["file_id"] == "f1"
    assert s.ancestor_calls[0]["limit"] == 20
    assert s.ancestor_calls[0]["max_ancestor_depth"] == 2


@pytest.mark.asyncio
async def test_expansion_swallows_per_call_errors():
    class BoomSearcher(FakeSearcher):
        async def get_related_chunks(self, **kwargs):
            raise RuntimeError("kaboom")

    s = BoomSearcher()
    r = SingleRetriever(searcher=s, include_related=True)
    initial = [Chunk(id="1", text="x", partition="p1", metadata={"relationship_id": "r1"})]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1"]


@pytest.mark.asyncio
async def test_expansion_swallows_ancestor_errors():
    class BoomSearcher(FakeSearcher):
        async def get_ancestor_chunks(self, **kwargs):
            raise RuntimeError("ancestor exploded")

    s = BoomSearcher()
    r = SingleRetriever(searcher=s, include_ancestors=True)
    initial = [Chunk(id="1", text="x", partition="p1", document_id="f1")]
    out = await r.expand_search_results(initial)
    assert [c.id for c in out] == ["1"]


def test_multi_query_retriever_rejects_missing_llm():
    s = FakeSearcher()
    with pytest.raises(ValueError, match="llm must be provided"):
        MultiQueryRetriever(searcher=s, llm=None, multi_query_template="{query} {k_queries}")


def test_hyde_retriever_rejects_missing_llm():
    s = FakeSearcher()
    with pytest.raises(ValueError, match="llm must be provided"):
        HyDeRetriever(searcher=s, llm=None, hyde_template="{question}")


@pytest.mark.asyncio
async def test_multi_query_retriever_caps_response_to_k_queries():
    """A non-compliant LLM that returns more variants than requested must
    not fan out additional searches."""
    s = FakeSearcher()
    llm = FakeLLM(response="Q1[SEP]Q2[SEP]Q3[SEP]Q4[SEP]Q5")
    r = MultiQueryRetriever(
        searcher=s,
        llm=llm,
        multi_query_template="{query} {k_queries}",
        k_queries=2,
    )
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["Q1", "Q2"]


@pytest.mark.asyncio
async def test_hyde_retriever_falls_back_to_seed_on_blank_generation():
    s = FakeSearcher()
    llm = FakeLLM(response="   \n\t  ")
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="{question}")
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["seed"]


@pytest.mark.asyncio
async def test_hyde_retriever_falls_back_to_seed_when_combine_and_blank():
    """Combine mode should also fall back to just [seed] on blank generation,
    not [blank, seed]."""
    s = FakeSearcher()
    llm = FakeLLM(response="")
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="{question}", combine=True)
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["seed"]


@pytest.mark.asyncio
async def test_expansion_dedupes_ancestor_fetches_per_file():
    """Two chunks from the same (partition, document_id) must enqueue a
    single ancestor fetch, not two."""

    class CountingSearcher(FakeSearcher):
        def __init__(self) -> None:
            super().__init__()
            self.ancestor_call_count = 0

        async def get_ancestor_chunks(self, **kwargs):
            self.ancestor_call_count += 1
            return list(self.ancestor_result)

    s = CountingSearcher()
    s.ancestor_result = [_chunk("99", document_id="f1")]
    r = SingleRetriever(searcher=s, include_ancestors=True)
    initial = [
        Chunk(id="1", text="x", partition="p1", document_id="f1"),
        Chunk(id="2", text="y", partition="p1", document_id="f1"),
        Chunk(id="3", text="z", partition="p1", document_id="f1"),
    ]
    await r.expand_search_results(initial)
    assert s.ancestor_call_count == 1


# --- #703 regression: LLM.chat returns a dict, not a str -------------------


def test_chat_content_extracts_assistant_message():
    assert chat_content({"choices": [{"message": {"content": "hello"}}]}) == "hello"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        None,
        "already a string",
    ],
)
def test_chat_content_degrades_to_empty_on_malformed_payload(payload):
    """A non-compliant provider must not crash retrieval — callers fall back
    to the original query when expansion yields nothing."""
    assert chat_content(payload) == ""


@pytest.mark.asyncio
async def test_multi_query_handles_dict_response_from_llm_port():
    """Regression for #703: the real ``LLM.chat`` returns a dict. Calling
    ``.split()`` on it raised AttributeError on every multiQuery search."""
    s = FakeSearcher()
    llm = FakeLLM("alpha[SEP]beta")
    r = MultiQueryRetriever(
        searcher=s,
        llm=llm,
        multi_query_template="generate {k_queries} variants of: {query}",
        k_queries=2,
    )
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_hyde_handles_dict_response_from_llm_port():
    """Regression for #703: ``.strip()`` on the dict raised AttributeError on
    every hyde search."""
    s = FakeSearcher()
    llm = FakeLLM("  hypothetical answer  ")
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="Answer: {question}")
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["hypothetical answer"]


@pytest.mark.asyncio
async def test_hyde_falls_back_to_seed_when_llm_returns_malformed_payload():
    s = FakeSearcher()
    llm = FakeLLM("")

    async def _malformed(messages, **kwargs):
        llm.chat_calls.append(messages)
        return {"unexpected": "shape"}

    llm.chat = _malformed
    r = HyDeRetriever(searcher=s, llm=llm, hyde_template="Answer: {question}")
    await r.retrieve(partition=["p1"], query="seed")
    assert s.multi_calls[0]["queries"] == ["seed"]
