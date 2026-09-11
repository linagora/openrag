"""Check catalog membership after retrieval, including chunk expansion."""

from core.models.chunk import Chunk
from core.observability.monitoring import ORPHAN_CHUNKS_DROPPED
from core.ports.document_repo import DocumentRepository
from core.retrieval.searcher import RetrievalSearcher


class CatalogSearcher(RetrievalSearcher):
    def __init__(self, searcher: RetrievalSearcher, document_repo: DocumentRepository) -> None:
        self._searcher = searcher
        self._document_repo = document_repo

    async def _filter(self, chunks: list[Chunk]) -> list[Chunk]:
        if not chunks:
            return []
        keys = {(chunk.partition, chunk.document_id) for chunk in chunks}
        # No fallback or cache: a catalog outage must never expose deleted files.
        existing = await self._document_repo.get_indexed_documents(keys)
        result = [chunk for chunk in chunks if (chunk.partition, chunk.document_id) in existing]
        ORPHAN_CHUNKS_DROPPED.inc(len(chunks) - len(result))
        return result

    async def search(self, *args, **kwargs) -> list[Chunk]:
        return await self._filter(await self._searcher.search(*args, **kwargs))

    async def multi_query_search(self, *args, **kwargs) -> list[Chunk]:
        return await self._filter(await self._searcher.multi_query_search(*args, **kwargs))

    async def get_related_chunks(self, *args, **kwargs) -> list[Chunk]:
        return await self._filter(await self._searcher.get_related_chunks(*args, **kwargs))

    async def get_ancestor_chunks(self, *args, **kwargs) -> list[Chunk]:
        return await self._filter(await self._searcher.get_ancestor_chunks(*args, **kwargs))
