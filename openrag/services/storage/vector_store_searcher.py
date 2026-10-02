"""``RetrievalSearcher`` backed directly by ``VectorStore`` + ``Embedder``.

Replaces ``MilvusRayShim`` — embeds queries in-process and calls the
``VectorStore`` without routing through a Ray actor.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from core.embeddings import Embedder
from core.models.chunk import Chunk, _coerce_chunk_type
from core.ports.document_repo import DocumentRepository
from core.retrieval.searcher import RetrievalSearcher, file_id_restriction
from core.utils.consts import RETRIEVAL_SCORE_KEYS, is_internal_metadata_key
from core.utils.logging import get_logger
from core.vector_stores import VectorStore
from core.vector_stores.vector_field import is_vector_field_key

logger = get_logger()


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
        "_id",
        "id",
        "score",
        "file_id",
        "partition",
        "page",
        "chunk_type",
        "$meta",
        *RETRIEVAL_SCORE_KEYS,
    }
    dynamic_metadata = row.get("$meta")
    dynamic_metadata = dynamic_metadata if isinstance(dynamic_metadata, dict) else {}
    metadata = {
        k: v
        for k, v in {**dynamic_metadata, **row}.items()
        if k not in skip and not is_vector_field_key(k) and not is_internal_metadata_key(k)
    }
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
        vector_field: str | Callable[[], str | None] | None = None,
    ) -> None:
        self._store = vector_store
        self._embedder = embedder
        self._document_repo = document_repo
        self._collection = collection
        # The dense field of this searcher's embedder. A callable is read on
        # every search, since the searcher can be built before the endpoint
        # registry is loaded.
        self._vector_field = vector_field

    def _field(self) -> str | None:
        return self._vector_field() if callable(self._vector_field) else self._vector_field

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
            vector_field=self._field(),
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
        field = self._field()
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
                    vector_field=field,
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

    async def get_surrounding_chunks(
        self,
        chunks: list[Chunk],
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        return await self._fetch_surrounding(chunks, allowed_file_ids=allowed_file_ids)

    async def get_csv_row_chunks(
        self,
        partition: str,
        file_id: str,
        row_number: int,
        allowed_file_ids: list[str] | None = None,
    ) -> list[Chunk]:
        """Return every stored continuation for one CSV row in part order."""
        if allowed_file_ids is not None and file_id not in allowed_file_ids:
            return []
        rows = await self._store.query_chunks_by_filter(
            self._collection,
            {
                "partition": partition,
                "file_id": file_id,
                "csv_row_number": row_number,
            },
        )
        chunks = [_dict_to_chunk(row) for row in rows]
        return sorted(chunks, key=_csv_part_sort_key)

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
        # A chunk's neighbours are in its own file (a file's chunks are
        # numbered in one batch), so each file's neighbours come from one query
        # scoped to its partition, the file and the section_ids asked for in
        # it. The partition scope keeps the lookup inside the tenant. The file
        # scope ignores another file that holds the same section_id, such as a
        # copy of this one in the same partition. Refs whose source chunk has
        # no partition or file are dropped, and so are those of a file outside
        # the caller's restriction: the source chunks passed it, so that only
        # guards a caller that passes chunks from outside it.
        allowed = set(allowed_file_ids) if allowed_file_ids is not None else None
        # (partition, file_id) → the section_ids asked for, as an ordered set.
        refs: dict[tuple[str, str], dict[Any, None]] = {}
        for c in chunks:
            if not c.partition or not c.document_id or (allowed is not None and c.document_id not in allowed):
                continue
            for sid in (c.metadata.get("prev_section_id"), c.metadata.get("next_section_id")):
                if sid is not None:
                    refs.setdefault((c.partition, c.document_id), {})[sid] = None
        if not refs:
            return []
        per_file = await asyncio.gather(
            *(
                self._store.query_chunks_by_filter(
                    self._collection,
                    {"partition": partition, "file_id": file_id, "section_id": list(section_ids)},
                )
                for (partition, file_id), section_ids in refs.items()
            )
        )
        results: list[Chunk] = []
        for (partition, file_id), rows in zip(refs, per_file, strict=True):
            hits_by_sid: dict[Any, list[dict[str, Any]]] = {}
            for r in rows:
                hits_by_sid.setdefault(r.get("section_id"), []).append(r)
            # An ID matching several chunks of one file means the file's IDs
            # are damaged: a Milvus partial upsert rounds IDs above 2**53, and
            # a few hundred neighbours then share one value. Taking every match
            # would return the whole document, so such a neighbour is skipped.
            if any(len(hits) > 1 for hits in hits_by_sid.values()):
                logger.warning(
                    "Skipped neighbour chunks whose section_id matches several chunks of the same file; "
                    "re-index the file to restore its neighbours",
                    partition=partition,
                    file_id=file_id,
                )
            results.extend(_dict_to_chunk(hits[0]) for hits in hits_by_sid.values() if len(hits) == 1)
        return results


def _csv_part_sort_key(chunk: Chunk) -> tuple[int, str]:
    """Order malformed or legacy CSV metadata after valid continuation parts."""
    try:
        return int(chunk.metadata.get("csv_part")), chunk.id
    except (TypeError, ValueError):
        return 2**31 - 1, chunk.id


__all__ = ["VectorStoreSearcher"]
