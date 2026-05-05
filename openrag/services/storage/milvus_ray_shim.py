"""Transitional ``RetrievalSearcher`` adapter wrapping the legacy Ray Milvus actor.

The new core retriever (``core/retrieval/retriever.py``) talks to a clean
ABC; this shim is what plugs the still-existing Ray god object into that
ABC for the duration of Phase 5–6. Once Phase 7 decomposes the Vectordb
actor into ``MilvusVectorStore`` + ``ChunkRepository`` this file goes away.

Conversion: the Ray actor returns LangChain ``Document`` objects with
metadata; we convert each one to a domain ``Chunk`` via
``Chunk.from_langchain``. Ray actor calls are routed through
``call_ray_actor_with_timeout`` so timeout/cancellation behavior matches
the rest of the legacy app — bypassing this helper was the regression
flagged in PR #352 (CodeRabbit).

The Ray imports are deferred to method bodies so this module is importable
in non-Ray contexts (tests, CLI tools).
"""

from __future__ import annotations

from typing import Any

from openrag.core.models.chunk import Chunk
from openrag.core.retrieval.searcher import RetrievalSearcher

# Match the legacy default in `components.pipeline` (config.ray.indexer.vectordb_timeout).
# The composition root can override via ``MilvusRayShim(actor, timeout=...)``.
DEFAULT_VECTORDB_TIMEOUT = 60.0


def _to_chunks(docs: list[Any]) -> list[Chunk]:
    """Convert LangChain Documents from the Ray actor into domain Chunks."""
    return [Chunk.from_langchain(d) for d in docs]


class MilvusRayShim(RetrievalSearcher):
    """Adapter exposing the Vectordb Ray actor as a ``RetrievalSearcher``.

    Args:
        actor: Ray actor handle (typically ``ray.get_actor("Vectordb",
               namespace="openrag")``). Accepts any object whose remote
               methods match the legacy Vectordb actor — useful for tests.
        timeout: Per-call timeout passed to ``call_ray_actor_with_timeout``.
    """

    def __init__(self, actor: Any, timeout: float = DEFAULT_VECTORDB_TIMEOUT) -> None:
        self._actor = actor
        self._timeout = timeout

    async def _call(self, future: Any, task_description: str) -> Any:
        # Deferred import: keeps this module importable without `ray` installed
        # (legacy `components.ray_utils` pulls in ray at import time).
        from components.ray_utils import call_ray_actor_with_timeout

        return await call_ray_actor_with_timeout(
            future=future,
            timeout=self._timeout,
            task_description=task_description,
        )

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
        docs = await self._call(
            self._actor.async_search.remote(
                query=query,
                partition=partition,
                top_k=top_k,
                filter=filter,
                filter_params=filter_params,
                similarity_threshold=similarity_threshold,
                with_surrounding_chunks=with_surrounding_chunks,
            ),
            task_description=f"async_search(partition={partition})",
        )
        return _to_chunks(docs)

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
        docs = await self._call(
            self._actor.async_multi_query_search.remote(
                queries=queries,
                partition=partition,
                top_k_per_query=top_k_per_query,
                filter=filter,
                filter_params=filter_params,
                similarity_threshold=similarity_threshold,
                with_surrounding_chunks=with_surrounding_chunks,
            ),
            task_description=f"async_multi_query_search(n={len(queries)}, partition={partition})",
        )
        return _to_chunks(docs)

    async def get_related_chunks(
        self,
        partition: str,
        relationship_id: str,
        limit: int,
    ) -> list[Chunk]:
        docs = await self._call(
            self._actor.get_related_chunks.remote(
                partition=partition,
                relationship_id=relationship_id,
                limit=limit,
            ),
            task_description=f"get_related_chunks(partition={partition}, rel={relationship_id})",
        )
        return _to_chunks(docs)

    async def get_ancestor_chunks(
        self,
        partition: str,
        file_id: str,
        limit: int,
        max_ancestor_depth: int | None = None,
    ) -> list[Chunk]:
        docs = await self._call(
            self._actor.get_ancestor_chunks.remote(
                partition=partition,
                file_id=file_id,
                limit=limit,
                max_ancestor_depth=max_ancestor_depth,
            ),
            task_description=f"get_ancestor_chunks(partition={partition}, file={file_id})",
        )
        return _to_chunks(docs)


def from_ray_namespace(
    name: str = "Vectordb",
    namespace: str = "openrag",
    timeout: float = DEFAULT_VECTORDB_TIMEOUT,
) -> MilvusRayShim:
    """Look up the Vectordb Ray actor by name and wrap it.

    Convenience for the composition root. The Ray import is deferred so
    importing this module without Ray installed (e.g. in unit tests of
    the retriever with a fake searcher) does not fail.
    """
    import ray

    return MilvusRayShim(ray.get_actor(name, namespace=namespace), timeout=timeout)
