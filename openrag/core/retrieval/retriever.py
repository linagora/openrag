"""Retriever strategies: Single, MultiQuery, HyDe.

Rewritten from ``components/retriever.py``. Differences from the legacy:

  * ``RetrievalSearcher`` (clean ABC) replaces ``get_vectordb()`` / Ray actor
    direct access. The retriever has no Ray imports.
  * ``LLM`` (clean ABC) replaces ``ChatOpenAI`` + LangChain chain assembly.
  * Prompt templates are passed in as strings. The DI layer loads them
    from disk via ``core/prompts/template_loader``.
  * Returns are domain ``Chunk`` objects, not LangChain ``Document``.

A ``retriever_registry`` is exposed so the composition root can pick a
strategy by name (``single`` / ``multiQuery`` / ``hyde``) per the
strategy doc's "every factory becomes a Registry" rule.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from itertools import chain as ichain
from typing import Any

from openrag.core.llm.llm import LLM
from openrag.core.models.chunk import Chunk
from openrag.core.prompts.query_rewriter import (
    build_hyde_prompt,
    build_multi_query_prompt,
    split_multi_query_response,
)
from openrag.core.retrieval.searcher import RetrievalSearcher
from openrag.core.utils.registry import Registry


class Retriever(ABC):
    """Common surface for all retrieval strategies."""

    @abstractmethod
    async def retrieve(
        self,
        partition: list[str],
        query: str,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        """Run the strategy and return scored chunks."""
        ...

    @abstractmethod
    async def expand_search_results(self, results: list[Chunk]) -> list[Chunk]:
        """Optionally enrich a result set with related/ancestor chunks."""
        ...


class BaseRetriever(Retriever):
    """Single-query retriever — the building block for the others."""

    def __init__(
        self,
        searcher: RetrievalSearcher,
        top_k: int = 6,
        similarity_threshold: float = 0.95,
        with_surrounding_chunks: bool = True,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 10,
        max_ancestor_depth: int | None = None,
        **_: Any,
    ) -> None:
        self.searcher = searcher
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.with_surrounding_chunks = with_surrounding_chunks
        self.include_related = include_related
        self.include_ancestors = include_ancestors
        self.related_limit = related_limit
        self.max_ancestor_depth = max_ancestor_depth
        self.expansion_enabled = include_related or include_ancestors

    async def retrieve(
        self,
        partition: list[str],
        query: str,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        return await self.searcher.search(
            query=query,
            partition=partition,
            top_k=self.top_k,
            filter=filter,
            filter_params=filter_params,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )

    async def expand_search_results(self, results: list[Chunk]) -> list[Chunk]:
        return await _expand_with_related_chunks(
            searcher=self.searcher,
            results=results,
            include_related=self.include_related,
            include_ancestors=self.include_ancestors,
            related_limit=self.related_limit,
            max_ancestor_depth=self.max_ancestor_depth,
        )


class SingleRetriever(BaseRetriever):
    """Default strategy — issues exactly one similarity search per query."""


class MultiQueryRetriever(BaseRetriever):
    """Generates K query variants via the LLM and unions their results."""

    def __init__(
        self,
        searcher: RetrievalSearcher,
        llm: LLM,
        multi_query_template: str,
        k_queries: int = 3,
        **kwargs: Any,
    ) -> None:
        super().__init__(searcher=searcher, **kwargs)
        if llm is None:
            raise ValueError("llm must be provided for MultiQueryRetriever")
        self.llm = llm
        self.multi_query_template = multi_query_template
        self.k_queries = k_queries

    async def _generate_queries(self, query: str) -> list[str]:
        prompt = build_multi_query_prompt(self.multi_query_template, query, self.k_queries)
        response = await self.llm.chat([{"role": "user", "content": prompt}])
        # Cap to k_queries — a non-compliant LLM response can otherwise fan
        # out far more searches than configured.
        queries = split_multi_query_response(response)[: self.k_queries]
        return queries or [query]

    async def retrieve(
        self,
        partition: list[str],
        query: str,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        queries = await self._generate_queries(query)
        return await self.searcher.multi_query_search(
            queries=queries,
            partition=partition,
            top_k_per_query=self.top_k,
            filter=filter,
            filter_params=filter_params,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )


class HyDeRetriever(BaseRetriever):
    """Generates a hypothetical answer document and searches with it.

    If ``combine`` is set, the original query is also issued and results
    are unioned via the searcher's multi-query path.
    """

    def __init__(
        self,
        searcher: RetrievalSearcher,
        llm: LLM,
        hyde_template: str,
        combine: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(searcher=searcher, **kwargs)
        if llm is None:
            raise ValueError("llm must be provided for HyDeRetriever")
        self.llm = llm
        self.hyde_template = hyde_template
        self.combine = combine

    async def get_hyde(self, query: str) -> str:
        prompt = build_hyde_prompt(self.hyde_template, query)
        return await self.llm.chat([{"role": "user", "content": prompt}])

    async def retrieve(
        self,
        partition: list[str],
        query: str,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Chunk]:
        hyde = (await self.get_hyde(query)).strip()
        if not hyde:
            queries = [query]
        else:
            queries = [hyde, query] if self.combine else [hyde]
        return await self.searcher.multi_query_search(
            queries=queries,
            partition=partition,
            top_k_per_query=self.top_k,
            filter=filter,
            filter_params=filter_params,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )


async def _expand_with_related_chunks(
    searcher: RetrievalSearcher,
    results: list[Chunk],
    include_related: bool,
    include_ancestors: bool,
    related_limit: int = 10,
    max_ancestor_depth: int | None = None,
) -> list[Chunk]:
    """Append related and/or ancestor chunks to a result set, deduplicated by id.

    Failures on individual related/ancestor lookups are logged and treated
    as empty results, matching legacy behavior so retrieval remains
    resilient to per-document errors.
    """
    if not results or (not include_related and not include_ancestors):
        return results

    seen_ids = {c.id for c in results if c.id}
    expanded: list[Chunk] = list(results)

    relationship_ids: set[tuple[str, str]] = set()
    file_infos: set[tuple[str, str]] = set()

    for c in results:
        if include_related:
            rel_id = c.metadata.get("relationship_id")
            if rel_id and c.partition:
                relationship_ids.add((c.partition, rel_id))
        if include_ancestors and c.partition and c.document_id:
            file_infos.add((c.partition, c.document_id))

    async def _safe_related(part: str, rel_id: str) -> list[Chunk]:
        try:
            return await searcher.get_related_chunks(partition=part, relationship_id=rel_id, limit=related_limit)
        except Exception:
            return []

    async def _safe_ancestors(part: str, file_id: str) -> list[Chunk]:
        try:
            return await searcher.get_ancestor_chunks(
                partition=part,
                file_id=file_id,
                limit=related_limit,
                max_ancestor_depth=max_ancestor_depth,
            )
        except Exception:
            return []

    tasks: list[asyncio.Future] = []
    if include_related:
        tasks.extend(_safe_related(part, rid) for part, rid in relationship_ids)
    if include_ancestors:
        tasks.extend(_safe_ancestors(part, fid) for part, fid in file_infos if part and fid)

    if tasks:
        all_results = await asyncio.gather(*tasks)
        for chunk in ichain.from_iterable(all_results):
            if chunk.id and chunk.id in seen_ids:
                continue
            if chunk.id:
                seen_ids.add(chunk.id)
            expanded.append(chunk)

    return expanded


# ---------------------------------------------------------------------------
# Registry — config-driven factory replacement
# ---------------------------------------------------------------------------
retriever_registry: Registry[Retriever] = Registry("retriever")
retriever_registry.register("single")(SingleRetriever)
retriever_registry.register("multiQuery")(MultiQueryRetriever)
retriever_registry.register("hyde")(HyDeRetriever)
