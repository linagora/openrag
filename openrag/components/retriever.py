# Import necessary modules and classes
from abc import ABC, abstractmethod
from typing import ClassVar

from components.prompts import HYDE_PROMPT, MULTI_QUERY_PROMPT
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from omegaconf import OmegaConf
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
        **kwargs,
    ) -> None:
        pass

    @abstractmethod
    async def retrieve(self, partition: list[str], query: str) -> list[Document]:
        pass


# Define the Simple Retriever class
class BaseRetriever(ABCRetriever):
    def __init__(self, top_k=6, similarity_threshold=0.95, with_surrounding_chunks=True, **kwargs):
        super().__init__(top_k, similarity_threshold, **kwargs)
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.with_surrounding_chunks = with_surrounding_chunks

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


class SingleRetriever(BaseRetriever):
    pass


class MultiQueryRetriever(BaseRetriever):
    def __init__(
        self,
        top_k=6,
        similarity_threshold=0.95,
        k_queries: int = 3,
        llm: ChatOpenAI = None,
        **kwargs,
    ):
        super().__init__(top_k, similarity_threshold, **kwargs)
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
        llm: ChatOpenAI = None,
        combine: bool = False,
        **kwargs,
    ):
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


class RelationshipAwareRetriever(BaseRetriever):
    """
    Retriever that expands search results with related and ancestor documents.

    This retriever performs standard semantic search, then optionally expands
    results to include:
    - Related chunks: Documents sharing the same relationship_id (e.g., email thread)
    - Ancestor chunks: Documents in the parent hierarchy (e.g., parent emails)

    Use cases:
    - Email threads: Find an email and include the full conversation context
    - Folder structures: Find a file and include related files in the same folder
    - Document hierarchies: Find a section and include parent document context
    """

    def __init__(
        self,
        top_k=6,
        similarity_threshold=0.95,
        include_related: bool = False,
        include_ancestors: bool = False,
        related_limit: int = 20,
        **kwargs,
    ):
        super().__init__(top_k, similarity_threshold, **kwargs)
        self.include_related = include_related
        self.include_ancestors = include_ancestors
        self.related_limit = related_limit

    async def retrieve(
        self,
        partition: list[str],
        query: str,
    ) -> list[Document]:
        # 1. Standard semantic search
        chunks = await super().retrieve(partition, query)

        # 2. Expand with related/ancestor chunks if enabled
        if self.include_related or self.include_ancestors:
            chunks = await self._expand_with_related(chunks)

        return chunks

    async def _expand_with_related(self, chunks: list[Document]) -> list[Document]:
        """Expand results with related and/or ancestor chunks."""
        if not chunks:
            return chunks

        db = get_vectordb()

        # Track what we already have to avoid duplicates
        seen_ids = {doc.metadata.get("_id") for doc in chunks}
        expanded_results = list(chunks)

        # Collect unique relationship_ids and file_ids from results
        relationship_ids = set()
        file_infos = []  # List of (partition, file_id) tuples

        for doc in chunks:
            metadata = doc.metadata
            if self.include_related and metadata.get("relationship_id"):
                relationship_ids.add((metadata.get("partition"), metadata.get("relationship_id")))
            if self.include_ancestors:
                file_infos.append((metadata.get("partition"), metadata.get("file_id")))

        # Fetch related chunks by relationship_id
        if self.include_related:
            for partition, rel_id in relationship_ids:
                if partition and rel_id:
                    try:
                        related_chunks = await db.get_related_chunks.remote(
                            partition=partition,
                            relationship_id=rel_id,
                            limit=self.related_limit,
                        )
                        for chunk in related_chunks:
                            chunk_id = chunk.metadata.get("_id")
                            if chunk_id and chunk_id not in seen_ids:
                                seen_ids.add(chunk_id)
                                expanded_results.append(chunk)
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch related chunks",
                            relationship_id=rel_id,
                            error=str(e),
                        )

        # Fetch ancestor chunks
        if self.include_ancestors:
            for partition, file_id in file_infos:
                if partition and file_id:
                    try:
                        ancestor_chunks = await db.get_ancestor_chunks.remote(
                            partition=partition,
                            file_id=file_id,
                            limit=self.related_limit,
                        )
                        for chunk in ancestor_chunks:
                            chunk_id = chunk.metadata.get("_id")
                            if chunk_id and chunk_id not in seen_ids:
                                seen_ids.add(chunk_id)
                                expanded_results.append(chunk)
                    except Exception as e:
                        logger.warning(
                            "Failed to fetch ancestor chunks",
                            file_id=file_id,
                            error=str(e),
                        )

        logger.debug(
            "Expanded results with related/ancestor chunks",
            original_count=len(chunks),
            expanded_count=len(expanded_results),
        )
        return expanded_results


class RetrieverFactory:
    RETRIEVERS: ClassVar[dict] = {
        "single": SingleRetriever,
        "multiQuery": MultiQueryRetriever,
        "hyde": HyDeRetriever,
        "relationshipAware": RelationshipAwareRetriever,
    }

    @classmethod
    def create_retriever(cls, config: OmegaConf) -> ABCRetriever:
        retreiverConfig = OmegaConf.to_container(config.retriever, resolve=True)

        retriever_type = retreiverConfig.pop("type")
        retriever_cls = RetrieverFactory.RETRIEVERS.get(retriever_type, None)

        if retriever_cls is None:
            raise ValueError(f"Unknown retriever type: {retriever_type}")

        retreiverConfig["llm"] = ChatOpenAI(**config.llm)
        return retriever_cls(**retreiverConfig)
