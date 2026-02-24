from types import SimpleNamespace

import pytest

from components.mcp.adapters import RayIndexerSearchGateway
from components.mcp.interfaces import SearchRequest


class _FakeAsearch:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    async def remote(self, **kwargs):
        self.calls.append(kwargs)
        return self.docs


class _FakeIndexer:
    def __init__(self, docs):
        self.asearch = _FakeAsearch(docs)


@pytest.mark.asyncio
async def test_ray_indexer_adapter_search_maps_results():
    docs = [SimpleNamespace(page_content="alpha", metadata={"_id": "1", "partition": "p1"})]
    fake_indexer = _FakeIndexer(docs)
    gateway = RayIndexerSearchGateway(indexer_getter=lambda: fake_indexer)

    request = SearchRequest(query="hello", partitions=["p1"], top_k=3, similarity_threshold=0.7)
    result = await gateway.search(request)

    assert len(result) == 1
    assert result[0].content == "alpha"
    assert result[0].metadata["_id"] == "1"

    sent = fake_indexer.asearch.calls[0]
    assert sent["query"] == "hello"
    assert sent["partition"] == ["p1"]
    assert sent["top_k"] == 3
    assert sent["similarity_threshold"] == 0.7
    assert sent["filter"] is None


@pytest.mark.asyncio
async def test_ray_indexer_adapter_passes_file_filter():
    fake_indexer = _FakeIndexer([])
    gateway = RayIndexerSearchGateway(indexer_getter=lambda: fake_indexer)

    request = SearchRequest(query="hello", partitions=["p1"], file_id="file-1")
    await gateway.search(request)

    sent = fake_indexer.asearch.calls[0]
    assert sent["filter"] == {"file_id": "file-1"}
