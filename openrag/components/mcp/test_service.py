import pytest

from components.mcp.interfaces import BaseSearchGateway, SearchRequest, SearchResult
from components.mcp.service import SearchToolService


class _FakeGateway(BaseSearchGateway):
    def __init__(self, results):
        self.results = results
        self.last_request = None

    async def search(self, request: SearchRequest):
        self.last_request = request
        return self.results


@pytest.mark.asyncio
async def test_service_defaults_and_response_shape():
    gateway = _FakeGateway([SearchResult(content="doc", metadata={"_id": "abc", "partition": "p1"})])
    service = SearchToolService(gateway=gateway, default_top_k=7, max_top_k=10, similarity_threshold=0.65)

    response = await service.search_documents(query="  test query  ", allowed_partitions=["all"])

    assert response["query"] == "test query"
    assert response["partitions"] == ["all"]
    assert response["top_k"] == 7
    assert response["count"] == 1
    assert response["documents"][0]["chunk_id"] == "abc"
    assert gateway.last_request is not None
    assert gateway.last_request.partitions == ["all"]
    assert gateway.last_request.similarity_threshold == 0.65


@pytest.mark.asyncio
async def test_service_raises_on_invalid_input():
    gateway = _FakeGateway([])
    service = SearchToolService(gateway=gateway)

    with pytest.raises(ValueError, match="Query cannot be empty"):
        await service.search_documents(query="   ", allowed_partitions=["all"])

    with pytest.raises(ValueError, match="top_k must be greater than 0"):
        await service.search_documents(query="ok", top_k=0, allowed_partitions=["all"])


@pytest.mark.asyncio
async def test_service_enforces_partition_scope():
    gateway = _FakeGateway([])
    service = SearchToolService(gateway=gateway)

    with pytest.raises(PermissionError, match="Access denied"):
        await service.search_documents(query="ok", partitions=["private"], allowed_partitions=["public"])

    response = await service.search_documents(query="ok", partitions=["all"], allowed_partitions=["public"])
    assert response["partitions"] == ["public"]


@pytest.mark.asyncio
async def test_service_caps_top_k():
    gateway = _FakeGateway([])
    service = SearchToolService(gateway=gateway, default_top_k=5, max_top_k=9)

    response = await service.search_documents(query="ok", top_k=1000, allowed_partitions=["all"])
    assert response["top_k"] == 9


@pytest.mark.asyncio
async def test_service_requires_auth_scope():
    gateway = _FakeGateway([])
    service = SearchToolService(gateway=gateway)

    with pytest.raises(PermissionError, match="Authentication context is missing"):
        await service.search_documents(query="ok")
