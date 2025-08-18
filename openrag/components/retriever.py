# Import necessary modules and classes
from abc import ABC, abstractmethod
from pathlib import Path
from langchain_core.documents.base import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from omegaconf import OmegaConf

from .indexer import BaseVectorDB
from .utils import load_sys_template

CRITERIAS = ["similarity"]


class ABCRetriever(ABC):
    """Abstract class for the base retriever."""

    @abstractmethod
    def __init__(
        self,
        criteria: str = "similarity",
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        **extra_args,
    ) -> None:
        pass

    @abstractmethod
    async def retrieve(
        self, partition: list[str], query: str, db: BaseVectorDB
    ) -> list[Document]:
        pass


# Define the Simple Retriever class
class BaseRetriever(ABCRetriever):
    def __init__(
        self,
        criteria: str = "similarity",
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        logger=None,
        **extra_args,
    ) -> None:
        """Constructs all the necessary attributes for the Retriever object.

        Args:
            criteria (str, optional): Retrieval criteria. Defaults to "similarity".
            top_k (int, optional): top_k most similar documents to retrieve. Defaults to 6.
        """
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        if criteria not in CRITERIAS:
            ValueError(f"Invalid type. Choose from {CRITERIAS}")
        self.criteria = criteria
        self.logger = logger

    async def retrieve(
        self, partition: list[str], query: str, db: BaseVectorDB
    ) -> list[Document]:
        chunks = await db.async_search.remote(
            query=query,
            partition=partition,
            top_k=self.top_k,
            similarity_threshold=self.similarity_threshold,
        )
        return chunks


class SingleRetreiver(BaseRetriever):
    def __init__(
        self,
        criteria: str = "similarity",
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        logger=None,
        **extra_args,
    ) -> None:
        super().__init__(criteria, top_k, similarity_threshold, logger, **extra_args)


class MultiQueryRetriever(BaseRetriever):
    def __init__(
        self,
        criteria: str = "similarity",
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        logger=None,
        **extra_args,
    ) -> None:
        """
        The MultiQueryRetriever class is a subclass of the Retriever class that retrieves relevant documents based on multiple queries.
        Given a query, multiple similar are generated with an llm. retrieval is done with each one them and finally a subset is chosen.

        Attributes
        ----------
        Args:
            criteria (str, optional): Retrieval criteria. Defaults to "similarity".
            top_k (int, optional): top_k most similar documents to retrieve. Defaults to 6.
            extra_args (dict): contains additionals arguments for this type of retriever.
        """
        super().__init__(criteria, top_k, similarity_threshold, logger, **extra_args)

        try:
            llm: ChatOpenAI = extra_args.get("llm")
            if not isinstance(llm, ChatOpenAI):
                raise TypeError(f"`llm` should be of type {ChatOpenAI}")

            k_queries = extra_args.get("k_queries")
            if not isinstance(k_queries, int):
                raise TypeError(f"`k_queries` should be of type {int}")
            self.k_queries = k_queries

            pmpt_tmpl_path = extra_args.get("prompts_dir") / extra_args.get(
                "prompt_tmpl"
            )
            multi_query_tmpl = load_sys_template(pmpt_tmpl_path)
            prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(
                multi_query_tmpl
            )
            self.generate_queries = (
                prompt | llm | StrOutputParser() | (lambda x: x.split("[SEP]"))
            )

        except Exception as e:
            raise KeyError(f"An Error has occured: {e}")

    async def retrieve(
        self, partition: list[str], query: str, db: BaseVectorDB
    ) -> list[Document]:
        # generate different perspectives of the query
        generated_queries = await self.generate_queries.ainvoke(
            {"query": query, "k_queries": self.k_queries}
        )
        chunks = await db.async_multi_query_search.remote(
            queries=generated_queries,
            partition=partition,
            top_k_per_query=self.top_k,
            similarity_threshold=self.similarity_threshold,
        )
        return chunks


class HyDeRetriever(BaseRetriever):
    def __init__(
        self,
        criteria: str = "similarity",
        top_k: int = 6,
        similarity_threshold: int = 0.95,
        logger=None,
        **extra_args,
    ) -> None:
        super().__init__(criteria, top_k, similarity_threshold, logger, **extra_args)

        try:
            llm = extra_args.get("llm")
            if not isinstance(llm, ChatOpenAI):
                raise TypeError(f"`llm` should be of type {ChatOpenAI}")

            pmpt_tmpl_path = extra_args.get("prompts_dir") / extra_args.get(
                "prompt_tmpl"
            )
            hyde_template = load_sys_template(pmpt_tmpl_path)
            prompt: ChatPromptTemplate = ChatPromptTemplate.from_template(hyde_template)

            self.generate_hyde = prompt | llm | StrOutputParser()
            self.combine = extra_args.get("combine", False)

        except Exception as e:
            raise ArithmeticError(f"An error occured: {e}")

    async def get_hyde(self, query: str):
        self.logger.debug("Generating HyDe Document")
        hyde_document = await self.generate_hyde.ainvoke({"query": query})
        return hyde_document

    async def retrieve(
        self, partition: list[str], query: str, db: BaseVectorDB
    ) -> list[Document]:
        hyde = await self.get_hyde(query)
        queries = [hyde]
        if self.combine:
            queries.append(query)

        return await db.async_multi_query_search.remote(
            queries=queries,
            partition=partition,
            top_k_per_query=self.top_k,
            similarity_threshold=self.similarity_threshold,
        )


class RetrieverFactory:
    RETRIEVERS = {
        "single": BaseRetriever,
        "multiQuery": MultiQueryRetriever,
        "hyde": HyDeRetriever,
    }

    @classmethod
    def create_retriever(cls, config: OmegaConf, logger) -> ABCRetriever:
        retreiverConfig = OmegaConf.to_container(config.retriever, resolve=True)
        retreiverConfig["logger"] = logger
        retreiverConfig["prompts_dir"] = Path(config.paths["prompts_dir"])

        retriever_type = retreiverConfig.pop("type")
        retriever_cls = RetrieverFactory.RETRIEVERS.get(retriever_type, None)
        if retriever_type is None:
            raise ValueError(f"Unknown retriever type: {retriever_type}")

        if retriever_type in ["hyde", "multiQuery"]:
            retreiverConfig["llm"] = ChatOpenAI(**config.vlm)

        return retriever_cls(**retreiverConfig)
