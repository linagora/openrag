"""Backward-compatibility shim — retriever strategies delegate to `openrag.core.retrieval`.

Phase 5A/5.15 status:

* `BaseRetriever` / `SingleRetriever` / `MultiQueryRetriever` / `HyDeRetriever`
  → adapters wrapping the corresponding `core.retrieval.retriever` strategies.
  The Ray actor is wrapped in a `MilvusRayShim` so the core retriever talks
  to a `RetrievalSearcher` port. The LLM (legacy `ChatOpenAI`) is wrapped in
  a `_LangChainLLMAdapter` so it fits the core `LLM` ABC.
* Output is converted from domain `Chunk` back to LangChain `Document` so
  legacy callers (RetrieverPipeline, RagPipeline) keep working unchanged.
* `_expand_with_related_chunks` → delegates to
  `core.retrieval.retriever._expand_with_related_chunks` with the same
  conversion at the boundary.
* `RetrieverFactory` is config-driven; the new code uses
  `retriever_registry`. Both coexist until Phase 8 cutover.

Scheduled for removal in Phase 12.
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from components.prompts import HYDE_PROMPT, MULTI_QUERY_PROMPT
from langchain_core.documents.base import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from openrag.core.llm.llm import LLM as _CoreLLM
from openrag.core.models.chunk import Chunk
from openrag.core.retrieval.retriever import (
    HyDeRetriever as _CoreHyDeRetriever,
)
from openrag.core.retrieval.retriever import (
    MultiQueryRetriever as _CoreMultiQueryRetriever,
)
from openrag.core.retrieval.retriever import (
    SingleRetriever as _CoreSingleRetriever,
)
from openrag.core.retrieval.retriever import (
    _expand_with_related_chunks as _core_expand,
)
from openrag.services.storage.milvus_ray_shim import MilvusRayShim

logger = get_logger()


# ---------------------------------------------------------------------------
# Adapters bridging legacy types (ChatOpenAI, Ray actor, Document) to core.
# ---------------------------------------------------------------------------
class _LangChainLLMAdapter(_CoreLLM):
    """Wraps a LangChain ``ChatOpenAI`` so it satisfies the core ``LLM`` ABC."""

    _ROLE_MAP: ClassVar[dict] = {"user": HumanMessage, "system": SystemMessage, "assistant": AIMessage}

    def __init__(self, lc_llm: ChatOpenAI) -> None:
        self._llm = lc_llm

    async def generate(self, prompt: str, **kwargs) -> str:
        out = await self._llm.ainvoke(prompt)
        return out.content if hasattr(out, "content") else str(out)

    async def chat(self, messages: list[dict[str, str]], **kwargs) -> str:
        lc_msgs = [self._ROLE_MAP[m["role"]](content=m["content"]) for m in messages]
        out = await self._llm.ainvoke(lc_msgs)
        return out.content


def _searcher() -> MilvusRayShim:
    """Wrap the legacy Vectordb Ray actor as a core ``RetrievalSearcher``."""
    return MilvusRayShim(get_vectordb())


def _to_documents(chunks: list[Chunk]) -> list[Document]:
    """Convert core ``Chunk`` objects back to LangChain ``Document``s for legacy callers."""
    return [c.to_langchain() for c in chunks]


def _from_documents(docs: list[Document]) -> list[Chunk]:
    """Convert legacy ``Document``s into core ``Chunk``s for the expansion helper."""
    return [Chunk.from_langchain(d) for d in docs]


# ---------------------------------------------------------------------------
# Legacy ABCs — preserved so existing isinstance / type hints keep working.
# ---------------------------------------------------------------------------
class ABCRetriever(ABC):
    """Abstract class for the base retriever."""

    @abstractmethod
    def __init__(
        self,
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 10,
        max_ancestor_depth: int | None = None,
        **kwargs,
    ) -> None:
        pass

    @abstractmethod
    async def retrieve(
        self, partition: list[str], query: str, filter: str | None = None, filter_params: dict | None = None
    ) -> list[Document]:
        pass

    async def expand_search_results(self, results: list[Document]) -> list[Document]:
        pass


class BaseRetriever(ABCRetriever):
    """Common adapter — instantiates a core retriever and converts I/O at the boundary."""

    _CORE_CLS: type = _CoreSingleRetriever

    def __init__(
        self,
        top_k: int = 6,
        similarity_threshold: float = 0.95,
        with_surrounding_chunks: bool = True,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 10,
        max_ancestor_depth: int | None = None,
        **kwargs,
    ) -> None:
        # Mirror legacy attributes so external callers reading them still work.
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.with_surrounding_chunks = with_surrounding_chunks
        self.include_related = include_related
        self.include_ancestors = include_ancestors
        self.related_limit = related_limit
        self.max_ancestor_depth = max_ancestor_depth
        self.expansion_enabled = include_related or include_ancestors
        self._core_kwargs = self._build_core_kwargs(kwargs)

    def _build_core_kwargs(self, extra: dict[str, Any]) -> dict[str, Any]:
        """Kwargs handed to the core retriever's constructor."""
        return {
            "top_k": self.top_k,
            "similarity_threshold": self.similarity_threshold,
            "with_surrounding_chunks": self.with_surrounding_chunks,
            "include_related": self.include_related,
            "include_ancestors": self.include_ancestors,
            "related_limit": self.related_limit,
            "max_ancestor_depth": self.max_ancestor_depth,
        }

    def _build_core_retriever(self):
        """Late-bind the core retriever so the Ray actor is only resolved on use."""
        return self._CORE_CLS(searcher=_searcher(), **self._core_kwargs)

    async def retrieve(
        self,
        partition: list[str],
        query: str,
        filter: str | None = None,
        filter_params: dict | None = None,
    ) -> list[Document]:
        chunks = await self._build_core_retriever().retrieve(
            partition=partition, query=query, filter=filter, filter_params=filter_params
        )
        return _to_documents(chunks)

    async def expand_search_results(self, results: list[Document]) -> list[Document]:
        expanded = await _core_expand(
            searcher=_searcher(),
            results=_from_documents(results),
            include_related=self.include_related,
            include_ancestors=self.include_ancestors,
            related_limit=self.related_limit,
            max_ancestor_depth=self.max_ancestor_depth,
        )
        return _to_documents(expanded)


class SingleRetriever(BaseRetriever):
    _CORE_CLS = _CoreSingleRetriever


class MultiQueryRetriever(BaseRetriever):
    _CORE_CLS = _CoreMultiQueryRetriever

    def __init__(
        self,
        top_k: int = 6,
        similarity_threshold: float = 0.95,
        with_surrounding_chunks: bool = True,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 10,
        max_ancestor_depth: int | None = None,
        k_queries: int = 3,
        llm: ChatOpenAI | None = None,
        **kwargs,
    ) -> None:
        if llm is None:
            raise ValueError("llm must be provided for MultiQueryRetriever")
        self.k_queries = k_queries
        self.llm = llm
        super().__init__(
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            with_surrounding_chunks=with_surrounding_chunks,
            include_related=include_related,
            include_ancestors=include_ancestors,
            related_limit=related_limit,
            max_ancestor_depth=max_ancestor_depth,
            **kwargs,
        )

    def _build_core_kwargs(self, extra: dict[str, Any]) -> dict[str, Any]:
        kw = super()._build_core_kwargs(extra)
        kw.update(
            llm=_LangChainLLMAdapter(self.llm),
            multi_query_template=MULTI_QUERY_PROMPT,
            k_queries=self.k_queries,
        )
        return kw


class HyDeRetriever(BaseRetriever):
    _CORE_CLS = _CoreHyDeRetriever

    def __init__(
        self,
        top_k: int = 6,
        similarity_threshold: float = 0.95,
        with_surrounding_chunks: bool = True,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 10,
        max_ancestor_depth: int | None = None,
        llm: ChatOpenAI | None = None,
        combine: bool = False,
        **kwargs,
    ) -> None:
        if llm is None:
            raise ValueError("llm must be provided for HyDeRetriever")
        self.combine = combine
        self.llm = llm
        super().__init__(
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            with_surrounding_chunks=with_surrounding_chunks,
            include_related=include_related,
            include_ancestors=include_ancestors,
            related_limit=related_limit,
            max_ancestor_depth=max_ancestor_depth,
            **kwargs,
        )

    def _build_core_kwargs(self, extra: dict[str, Any]) -> dict[str, Any]:
        kw = super()._build_core_kwargs(extra)
        kw.update(
            llm=_LangChainLLMAdapter(self.llm),
            hyde_template=HYDE_PROMPT,
            combine=self.combine,
        )
        return kw

    async def get_hyde(self, query: str) -> str:
        # Preserved for legacy callers / tests that introspect this method.
        return await self._build_core_retriever().get_hyde(query)


# ---------------------------------------------------------------------------
# Legacy free function — preserved for external callers; routes through core.
# ---------------------------------------------------------------------------
async def _expand_with_related_chunks(
    db,
    results: list[Document],
    include_related: bool,
    include_ancestors: bool,
    related_limit: int = 10,
    max_ancestor_depth: int | None = None,
) -> list[Document]:
    """Backward-compat free-function — delegates to the core expansion helper.

    The legacy callers pass a Ray actor via ``db``; we wrap it in
    ``MilvusRayShim`` to satisfy the core ``RetrievalSearcher`` port.
    """
    expanded = await _core_expand(
        searcher=MilvusRayShim(db),
        results=_from_documents(results),
        include_related=include_related,
        include_ancestors=include_ancestors,
        related_limit=related_limit,
        max_ancestor_depth=max_ancestor_depth,
    )
    return _to_documents(expanded)


class RetrieverFactory:
    RETRIEVERS: ClassVar[dict] = {
        "single": SingleRetriever,
        "multiQuery": MultiQueryRetriever,
        "hyde": HyDeRetriever,
    }

    @classmethod
    def create_retriever(cls, config) -> ABCRetriever:
        retrieverConfig = config.retriever.model_dump()
        retriever_type = retrieverConfig.pop("type")
        retriever_cls = RetrieverFactory.RETRIEVERS.get(retriever_type, None)

        if retriever_cls is None:
            raise ValueError(f"Unknown retriever type: {retriever_type}")

        retrieverConfig["llm"] = ChatOpenAI(**config.llm.model_dump())
        return retriever_cls(**retrieverConfig)
