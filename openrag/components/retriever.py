# Import necessary modules and classes
from abc import ABC, abstractmethod

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
    def __init__(
        self, top_k=6, similarity_threshold=0.95, with_surrounding_chunks=True, **kwargs
    ):
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

        prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(
            MULTI_QUERY_PROMPT
        )
        self.generate_queries = (
            prompt | llm | StrOutputParser() | (lambda x: x.split("[SEP]"))
        )

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


class RetrieverFactory:
    RETRIEVERS = {
        "single": SingleRetriever,
        "multiQuery": MultiQueryRetriever,
        "hyde": HyDeRetriever,
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
