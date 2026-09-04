"""``RetrievalSearcher`` backed directly by ``VectorStore`` + ``Embedder``.

Replaces ``MilvusRayShim`` — embeds queries in-process and calls the
``VectorStore`` without routing through a Ray actor.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from core.embeddings import Embedder
from core.models.chunk import Chunk, _coerce_chunk_type
from core.ports.document_repo import DocumentRepository
from core.retrieval.searcher import RetrievalSearcher, file_id_restriction
from core.utils.consts import RETRIEVAL_SCORE_KEYS, is_internal_metadata_key
from core.vector_stores import VectorStore


def _dict_to_chunk(row: dict[str, Any]) -> Chunk:
    """Convert a VectorStore result dict to a domain Chunk.

    ``search()`` returns ``"id"`` (string already stringified by the store);
    ``query_chunks_by_filter()`` returns ``"_id"`` (raw Milvus INT64).
    """
    raw_id = row.get("id") or row.get("_id")
    chunk_id = str(raw_id) if raw_id is not None else str(uuid.uuid4())
    # Score keys are dropped too: they are request-scoped (see
    # ``RETRIEVAL_SCORE_KEYS``), so a persisted one is never this query's score.
    skip = {
        "text",
        "vector",
        "_id",
        "id",
        "score",
        "file_id",
        "partition",
        "page",
        "chunk_type",
        *RETRIEVAL_SCORE_KEYS,
    }
    metadata = {k: v for k, v in row.items() if k not in skip and not is_internal_metadata_key(k)}
    return Chunk(
        id=chunk_id,
        document_id=row.get("file_id", ""),
        text=row.get("text", ""),
        partition=row.get("partition", "default"),
        page_number=row.get("page"),
        chunk_type=_coerce_chunk_type(row.get("chunk_type", "text")),
        metadata=metadata,
    )


class VectorStoreSearcher(RetrievalSearcher):
    """``RetrievalSearcher`` that uses ``VectorStore`` + ``Embedder`` directly.

    This replaces the transitional ``MilvusRayShim`` used during Phase 8.
    Queries are embedded in-process; surrounding / related / ancestor chunk
    lookups go through ``VectorStore.query_chunks_by_filter``.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: Embedder,
        document_repo: DocumentRepository,
        collection: str,
    ) -> None:
        self._store = vector_store
        self._embedder = embedder
        self._document_repo = document_repo
        self._collection = collection

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
        (embedding,) = await self._embedder.embed([query])
        filters: dict[str, Any] = {"partition": partition}
        if filter:
            filters["expr"] = filter
        if filter_params:
            filters.update(filter_params)
        results = await self._store.search(
            embedding=embedding,
            query_text=query,
            collection=self._collection,
            filters=filters,
            top_k=top_k,
            similarity_threshold=similarity_threshold or None,
        )
        chunks = [_dict_to_chunk(r) for r in results]
        if with_surrounding_chunks and chunks:
            surrounding = await self._fetch_surrounding(chunks, allowed_file_ids=file_id_restriction(filter_params))
            seen = {c.id for c in chunks}
            chunks.extend(c for c in surrounding if c.id not in seen)
        return chunks

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
        embeddings = await self._embedder.embed(queries)
        filters: dict[str, Any] = {"partition": partition}
        if filter:
            filters["expr"] = filter
        if filter_params:
            filters.update(filter_params)
        per_query = await asyncio.gather(
            *[
                self._store.search(
                    embedding=emb,
                    query_text=q,
                    collection=self._collection,
                    filters=filters,
                    top_k=top_k_per_query,
                    similarity_threshold=similarity_threshold or None,
                )
                for emb, q in zip(embeddings, queries)
            ]
        )
        seen_ids: set[str] = set()
        chunks: list[Chunk] = []
        for results in per_query:
            for r in results:
                c = _dict_to_chunk(r)
                if c.id not in seen_ids:
                    seen_ids.add(c.id)
                    chunks.append(c)
        if with_surrounding_chunks and chunks:
            surrounding = await self._fetch_surrounding(chunks, allowed_file_ids=file_id_restriction(filter_params))
            chunks.extend(c for c in surrounding if c.id not in seen_ids)
        return chunks

    async def get_related_chunks(
        self,
        partition: str,
        relationship_id: str,
        limit: int,
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        file_ids = await self._document_repo.get_file_ids_by_relationship(
            partition=partition, relationship_id=relationship_id
        )
        if allowed_file_ids is not None:
            allowed = set(allowed_file_ids)
            file_ids = [f for f in file_ids if f in allowed]
        if not file_ids:
            return []
        rows = await self._store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": file_ids},
        )
        return [_dict_to_chunk(r) for r in rows[:limit]]

    async def get_ancestor_chunks(
        self,
        partition: str,
        file_id: str,
        limit: int,
        max_ancestor_depth: int | None = None,
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        ancestor_ids = await self._document_repo.get_ancestor_file_ids(
            partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
        )
        if allowed_file_ids is not None:
            allowed = set(allowed_file_ids)
            ancestor_ids = [f for f in ancestor_ids if f in allowed]
        if not ancestor_ids:
            return []
        rows = await self._store.query_chunks_by_filter(
            self._collection,
            {"partition": partition, "file_id": ancestor_ids},
        )
        return [_dict_to_chunk(r) for r in rows[:limit]]

    async def _fetch_surrounding(self, chunks: list[Chunk], allowed_file_ids: list[str] | None = None) -> list[Chunk]:
        # section_id is only unique within a partition, so the lookup MUST be
        # scoped to each source chunk's partition; otherwise a neighbouring
        # section_id could resolve to another tenant's chunk (cross-tenant leak,
        # N6). Group section_ids by partition and query each partition
        # separately; drop refs whose source chunk has no partition.
        by_partition: dict[str, list] = {}
        for c in chunks:
            if not c.partition:
                continue
            for sid in (c.metadata.get("prev_section_id"), c.metadata.get("next_section_id")):
                if sid is not None:
                    by_partition.setdefault(c.partition, []).append(sid)
        if not by_partition:
            return []
        allowed = set(allowed_file_ids) if allowed_file_ids is not None else None
        results: list[Chunk] = []
        for partition, section_ids in by_partition.items():
            rows = await self._store.query_chunks_by_filter(
                self._collection,
                {"section_id": section_ids, "partition": partition},
            )
            if allowed is not None:
                # A neighbouring section can belong to an adjacent file that sits
                # outside the caller's workspace/file scope — filter it out rather
                # than trusting section adjacency alone (workspace scoping, #706).
                rows = [r for r in rows if r.get("file_id") in allowed]
            results.extend(_dict_to_chunk(r) for r in rows)
        return results


__all__ = ["VectorStoreSearcher"]
