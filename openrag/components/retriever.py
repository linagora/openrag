# Import necessary modules and classes
import asyncio
from abc import ABC, abstractmethod
from itertools import chain
from typing import ClassVar

from components.prompts import HYDE_PROMPT, MULTI_QUERY_PROMPT
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from utils.dependencies import get_vectordb
from utils.logger import get_logger

logger = get_logger()


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
    async def retrieve(self, partition: list[str], query: str) -> list[Document]:
        pass

    async def expand_search_results(self, results: list[Document]) -> list[Document]:
        pass


# Define the Simple Retriever class
class BaseRetriever(ABCRetriever):
    def __init__(
        self,
        top_k=6,
        similarity_threshold=0.95,
        with_surrounding_chunks=True,
        include_related=False,
        include_ancestors=False,
        related_limit=10,
        max_ancestor_depth: int | None = None,
        **kwargs,
    ):
        super().__init__(
            top_k,
            similarity_threshold,
            include_related=include_related,
            include_ancestors=include_ancestors,
            related_limit=related_limit,
            max_ancestor_depth=max_ancestor_depth,
            **kwargs,
        )
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
    ) -> list[Document]:
        db = get_vectordb()
        chunks = await db.async_search.remote(
            query=query,
            partition=partition,
            top_k=self.top_k,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )
        return chunks

    async def expand_search_results(self, results: list[Document]) -> list[Document]:
        """Expand search results with related and ancestor chunks."""
        db = get_vectordb()
        return await _expand_with_related_chunks(
            db=db,
            results=results,
            include_related=self.include_related,
            include_ancestors=self.include_ancestors,
            related_limit=self.related_limit,
            max_ancestor_depth=self.max_ancestor_depth,
        )


class SingleRetriever(BaseRetriever):
    pass


class MultiQueryRetriever(BaseRetriever):
    def __init__(
        self,
        top_k=6,
        similarity_threshold=0.95,
        with_surrounding_chunks=True,
        include_related=False,
        include_ancestors=False,
        related_limit=10,
        max_ancestor_depth=None,
        k_queries: int = 3,
        llm: ChatOpenAI = None,
        **kwargs,
    ):
        super().__init__(
            top_k,
            similarity_threshold,
            with_surrounding_chunks,
            include_related,
            include_ancestors,
            related_limit,
            max_ancestor_depth,
            **kwargs,
        )

        self.k_queries = k_queries
        self.llm = llm

        if llm is None:
            raise ValueError("llm must be provided for MultiQueryRetriever")

        prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(MULTI_QUERY_PROMPT)
        self.generate_queries = prompt | llm | StrOutputParser() | (lambda x: x.split("[SEP]"))

    async def retrieve(self, partition: list[str], query: str) -> list[Document]:
        db = get_vectordb()
        logger.debug("Generating multiple queries", k_queries=self.k_queries)
        generated_queries = await self.generate_queries.ainvoke(
            {
                "query": query,
                "k_queries": self.k_queries,
            }
        )
        chunks = await db.async_multi_query_search.remote(
            queries=generated_queries,
            partition=partition,
            top_k_per_query=self.top_k,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )
        return chunks


class HyDeRetriever(BaseRetriever):
    def __init__(
        self,
        top_k=6,
        similarity_threshold=0.95,
        with_surrounding_chunks=True,
        include_related=False,
        include_ancestors=False,
        related_limit=10,
        max_ancestor_depth=None,
        llm: ChatOpenAI = None,
        combine: bool = False,
        **kwargs,
    ):
        super().__init__(
            top_k,
            similarity_threshold,
            with_surrounding_chunks,
            include_related,
            include_ancestors,
            related_limit,
            max_ancestor_depth,
            **kwargs,
        )

        super().__init__(top_k, similarity_threshold, **kwargs)
        if llm is None:
            raise ValueError("llm must be provided for HyDeRetriever")

        self.combine = combine
        self.llm = llm

        prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(HYDE_PROMPT)
        self.hyde_generator = prompt | llm | StrOutputParser()

    async def get_hyde(self, query: str):
        logger.debug("Generating HyDe Document")
        hyde_document = await self.hyde_generator.ainvoke({"query": query})
        return hyde_document

    async def retrieve(self, partition: list[str], query: str) -> list[Document]:
        db = get_vectordb()
        hyde = await self.get_hyde(query)
        queries = [hyde]
        if self.combine:
            queries.append(query)

        return await db.async_multi_query_search.remote(
            queries=queries,
            partition=partition,
            top_k_per_query=self.top_k,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )


async def _expand_with_related_chunks(
    db,
    results: list[Document],
    include_related: bool,
    include_ancestors: bool,
    related_limit: int = 10,
    max_ancestor_depth: int | None = None,
) -> list[Document]:
    """Expand results with related and/or ancestor chunks."""
    if not results or (not include_related and not include_ancestors):
        return results

    # Track what we already have to avoid duplicates
    seen_ids = {doc.metadata.get("_id") for doc in results}
    expanded_results = list(results)

    # Collect unique relationship_ids and file_ids from results
    relationship_ids = set()
    file_infos = []  # List of (partition, file_id) tuples

    for doc in results:
        metadata = doc.metadata
        if include_related and metadata.get("relationship_id"):
            relationship_ids.add((metadata.get("partition"), metadata.get("relationship_id")))
        if include_ancestors:
            file_infos.append((metadata.get("partition"), metadata.get("file_id")))

    # Create tasks for parallel fetching
    async def fetch_related(partition: str, rel_id: str) -> list[Document]:
        """Fetch related chunks with error handling."""
        try:
            return await db.get_related_chunks.remote(
                partition=partition,
                relationship_id=rel_id,
                limit=related_limit,
            )
        except Exception as e:
            logger.warning(
                "Failed to fetch related chunks",
                relationship_id=rel_id,
                error=str(e),
            )
            return []

    async def fetch_ancestors(partition: str, file_id: str) -> list[Document]:
        """Fetch ancestor chunks with error handling."""
        try:
            return await db.get_ancestor_chunks.remote(
                partition=partition,
                file_id=file_id,
                limit=related_limit,
                max_ancestor_depth=max_ancestor_depth,
            )
        except Exception as e:
            logger.warning(
                "Failed to fetch ancestor chunks",
                file_id=file_id,
                error=str(e),
            )
            return []

    # Build list of tasks for parallel execution
    tasks = []

    if include_related:
        tasks.extend(fetch_related(partition, rel_id) for partition, rel_id in relationship_ids if partition and rel_id)

    if include_ancestors:
        tasks.extend(fetch_ancestors(partition, file_id) for partition, file_id in file_infos if partition and file_id)

    # Execute all tasks in parallel
    if tasks:
        all_results = await asyncio.gather(*tasks)
        for chunk in chain.from_iterable(all_results):
            chunk_id = chunk.metadata.get("_id")
            if chunk_id and chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                expanded_results.append(chunk)

    logger.debug(
        "Expanded results with related/ancestor chunks",
        original_count=len(results),
        expanded_count=len(expanded_results),
    )
    return expanded_results


class RetrieverFactory:
    RETRIEVERS: ClassVar[dict] = {
        "single": SingleRetriever,
        "multiQuery": MultiQueryRetriever,
        "hyde": HyDeRetriever,
    }

    @classmethod
    def create_retriever(cls, config) -> ABCRetriever:
        retreiverConfig = config.retriever.model_dump()

        retriever_type = retreiverConfig.pop("type")
        retriever_cls = RetrieverFactory.RETRIEVERS.get(retriever_type, None)

        if retriever_cls is None:
            raise ValueError(f"Unknown retriever type: {retriever_type}")

        retreiverConfig["llm"] = ChatOpenAI(**config.llm.model_dump())
        return retriever_cls(**retreiverConfig)
