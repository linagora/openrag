"""Check catalog membership after retrieval, including chunk expansion."""

from __future__ import annotations

from itertools import islice
from threading import Lock
from time import monotonic

from core.models.chunk import Chunk
from core.observability.monitoring import ORPHAN_CHUNKS_DROPPED
from core.ports.document_repo import DocumentRepository
from core.retrieval.searcher import RetrievalSearcher
from core.utils.logging import get_logger

logger = get_logger()

# Shared across named searchers so creating a new instance cannot bypass the
# warning budget. This stores no document identities or catalog decisions.
_orphan_warning_lock = Lock()
_next_orphan_warning_at = 0.0


def _warn_orphans(keys: set[tuple[str, str]], dropped_chunks: int) -> None:
    global _next_orphan_warning_at
    with _orphan_warning_lock:
        now = monotonic()
        if now < _next_orphan_warning_at:
            return
        _next_orphan_warning_at = now + 60.0
    logger.bind(
        dropped_chunks=dropped_chunks,
        dropped_files=len(keys),
        orphaned_files_sample=[
            {"partition": partition[:128], "file_id": file_id[:128]} for partition, file_id in islice(keys, 10)
        ],
    ).warning(
        "Dropped chunks absent from the catalog; file keys are sampled and truncated, warnings limited to once per minute"
    )


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
        dropped = len(chunks) - len(result)
        ORPHAN_CHUNKS_DROPPED.inc(dropped)
        if dropped:
            _warn_orphans(keys - existing.keys(), dropped)
        return result

    async def search(
        self,
        query: str,
        partition: list[str],
        top_k: int,
        filter: str | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float = 0.0,
        with_surrounding_chunks: bool = True,
    ) -> list[Chunk]:
        return await self._filter(
            await self._searcher.search(
                query=query,
                partition=partition,
                top_k=top_k,
                filter=filter,
                filter_params=filter_params,
                similarity_threshold=similarity_threshold,
                with_surrounding_chunks=with_surrounding_chunks,
            )
        )

    async def multi_query_search(
        self,
        queries: list[str],
        partition: list[str],
        top_k_per_query: int,
        filter: str | None = None,
        filter_params: dict | None = None,
        similarity_threshold: float = 0.0,
        with_surrounding_chunks: bool = True,
    ) -> list[Chunk]:
        return await self._filter(
            await self._searcher.multi_query_search(
                queries=queries,
                partition=partition,
                top_k_per_query=top_k_per_query,
                filter=filter,
                filter_params=filter_params,
                similarity_threshold=similarity_threshold,
                with_surrounding_chunks=with_surrounding_chunks,
            )
        )

    async def get_related_chunks(
        self,
        partition: str,
        relationship_id: str,
        limit: int,
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        return await self._filter(
            await self._searcher.get_related_chunks(
                partition=partition,
                relationship_id=relationship_id,
                limit=limit,
                allowed_file_ids=allowed_file_ids,
            )
        )

    async def get_ancestor_chunks(
        self,
        partition: str,
        file_id: str,
        limit: int,
        max_ancestor_depth: int | None = None,
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        return await self._filter(
            await self._searcher.get_ancestor_chunks(
                partition=partition,
                file_id=file_id,
                limit=limit,
                max_ancestor_depth=max_ancestor_depth,
                allowed_file_ids=allowed_file_ids,
            )
        )


__all__ = ["CatalogSearcher"]
