from collections.abc import Callable

from components.mcp.interfaces import BaseSearchGateway, SearchRequest, SearchResult
from utils.dependencies import get_indexer


class RayIndexerSearchGateway(BaseSearchGateway):
    def __init__(self, indexer_getter: Callable = get_indexer):
        self._indexer_getter = indexer_getter

    async def search(self, request: SearchRequest) -> list[SearchResult]:
        indexer = self._indexer_getter()

        filter_payload = {"file_id": request.file_id} if request.file_id else None
        docs = await indexer.asearch.remote(
            query=request.query,
            partition=request.partitions,
            top_k=request.top_k,
            similarity_threshold=request.similarity_threshold,
            filter=filter_payload,
        )

        return [SearchResult(content=doc.page_content, metadata=dict(doc.metadata)) for doc in docs]
