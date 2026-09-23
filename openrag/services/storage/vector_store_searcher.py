"""``RetrievalSearcher`` backed directly by ``VectorStore`` + ``Embedder``.

Replaces ``MilvusRayShim`` — embeds queries in-process and calls the
``VectorStore`` without routing through a Ray actor.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from time import perf_counter
from typing import Any

from core.embeddings import Embedder
from core.models.chunk import Chunk, _coerce_chunk_type
from core.ports.document_repo import DocumentRepository
from core.retrieval.searcher import RetrievalSearcher, file_id_restriction
from core.retrieval.trace import RetrievalTraceBuilder
from core.utils.consts import RETRIEVAL_SCORE_KEYS, is_internal_metadata_key
from core.vector_stores import VectorStore
from core.vector_stores.vector_field import is_vector_field_key

_STORE_TRACE_STAGES = (
    "dense_before_threshold",
    "dense_after_threshold",
    "sparse",
    "hybrid_fused",
)


def _merge_store_traces(parent: RetrievalTraceBuilder, children: list[RetrievalTraceBuilder]) -> None:
    """Merge completed per-query traces in query order, never completion order."""
    for child in children:
        parent.record_query_trace(child)
    for stage_name in _STORE_TRACE_STAGES:
        stages = [child.stages[stage_name] for child in children if child.stages[stage_name].status != "not_run"]
        if not stages:
            continue
        statuses = {stage.status for stage in stages}
        if "error" in statuses:
            status = "error"
        elif "complete" in statuses:
            status = "complete"
        else:
            status = "unavailable"
        durations = [stage.duration_seconds for stage in stages if stage.duration_seconds is not None]
        parent.record_stage(
            stage_name,
            status=status,
            candidates=[candidate for stage in stages for candidate in stage.candidates],
            candidate_count=sum(stage.candidate_count for stage in stages),
            duration_seconds=max(durations) if durations else None,
            error="sub-query diagnostics failed" if status == "error" else None,
        )
    for key in sorted({key for child in children for key in child.timings}):
        parent.timings[key] = max(child.timings[key] for child in children if key in child.timings)
    for child in children:
        parent.errors.extend(child.errors)


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
        *RETRIEVAL_SCORE_KEYS,
    }
    metadata = {
        k: v for k, v in row.items() if k not in skip and not is_vector_field_key(k) and not is_internal_metadata_key(k)
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
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        embedding_started = perf_counter() if trace is not None else None
        (embedding,) = await self._embedder.embed([query])
        if trace is not None and embedding_started is not None:
            trace.timings["embedding"] = perf_counter() - embedding_started
        filters: dict[str, Any] = {"partition": partition}
        if filter:
            filters["expr"] = filter
        if filter_params:
            filters.update(filter_params)
        trace_kwargs = {}
        if trace is not None:
            trace_kwargs["trace"] = trace
        results = await self._store.search(
            embedding=embedding,
            query_text=query,
            collection=self._collection,
            filters=filters,
            top_k=top_k,
            similarity_threshold=similarity_threshold or None,
            vector_field=self._field(),
            **trace_kwargs,
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
        trace: RetrievalTraceBuilder | None = None,
    ) -> list[Chunk]:
        embedding_started = perf_counter() if trace is not None else None
        embeddings = await self._embedder.embed(queries)
        if trace is not None and embedding_started is not None:
            trace.timings["embedding"] = perf_counter() - embedding_started
        field = self._field()
        filters: dict[str, Any] = {"partition": partition}
        if filter:
            filters["expr"] = filter
        if filter_params:
            filters.update(filter_params)
        query_traces = (
            [
                RetrievalTraceBuilder(request_id=f"{trace.request_id}:subquery:{index}", original_query=query)
                for index, query in enumerate(queries)
            ]
            if trace is not None
            else []
        )
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
                    **({"trace": query_traces[index]} if trace is not None else {}),
                )
                for index, (emb, q) in enumerate(zip(embeddings, queries, strict=True))
            ]
        )
        if trace is not None:
            _merge_store_traces(trace, query_traces)
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
        if limit <= 0:
            return []
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
            limit=limit,
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
        if limit <= 0:
            return []
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
            limit=limit,
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
