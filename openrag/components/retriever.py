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
        log = logger.bind(query=query, partition=partition)

        db = get_vectordb()
        chunks = await db.async_search.remote(
            query=query,
            partition=partition,
            top_k=self.top_k,
            similarity_threshold=self.similarity_threshold,
            with_surrounding_chunks=self.with_surrounding_chunks,
        )

        extra_documents = []
        file_ids = set()
        for chunk in chunks:
            file_ids.add(chunk.metadata['file_id'])

        for file_id in file_ids:
            linked_documents = await db.get_related_files.remote(file_id, 'parent', partition)
            log.info(f'Got {len(linked_documents)} chunks from documents linked with {file_id}')
            for linked_file_id in set([ d.metadata['file_id'] for d in linked_documents ]):
                log.info("Linked : " + linked_file_id)

            extra_documents.extend(linked_documents)

        chunks.extend(extra_documents)

        for file_id in set( [ d.metadata['file_id'] for d in chunks ] ):
            log.info(f'Found: {file_id}')

        return chunks


class EmailRetriever(BaseRetriever):
    def __init__(self, top_k=6, similarity_threshold=0.95, **kwargs):
        super().__init__(top_k, similarity_threshold, **kwargs)

    def get_parent_id(self, doc):
        log = logger.bind(query="get_parent_id", partition="get_parent_id")

        try:
            for rel in doc.metadata['rels']:
                if rel['type'] in ['parent']:
                    return rel['target']
        except Exception as e:
            log.error(f'get_parent_id failed: {e}')
            raise

        log.info(f'No parent found: {doc.metadata["file_id"]}')
        return None

    def get_branch(self, docs, curr_chunk):
        log = logger.bind(query="get_branch", partition="get_branch")

        id2doc = {}
        id2parent_id = {}
        id2child_ids = {}

        all_docs = docs + [ curr_chunk ]

        for doc in all_docs:
            id2doc[doc.metadata['file_id']] = doc
            parent_id = self.get_parent_id(doc)
            id2parent_id[doc.metadata['file_id']] = parent_id
            if parent_id is not None and parent_id != doc.metadata['file_id']:
                if parent_id not in id2child_ids:
                    id2child_ids[parent_id] = []
                id2child_ids[parent_id].append(doc.metadata['file_id'])

        # From current to root
        curr_id = curr_chunk.metadata['file_id']
        subtree = []
        while curr_id != id2parent_id[curr_id]:
            curr_id = id2parent_id[curr_id]
            if curr_id is None:
                break
            subtree.append(curr_id)

        # From current to leaves
        q = []
        q.extend(id2child_ids[curr_chunk.metadata['file_id']])
        while len(q) > 0:
            curr_id = q.pop(0)
            subtree.append(curr_id)
            if curr_id in id2child_ids:
                q.extend(id2child_ids[curr_id])

        return [ id2doc[doc_id] for doc_id in subtree ]

    async def retrieve(
        self,
        partition: list[str],
        query: str,
    ) -> list[Document]:
        log = logger.bind(query=query, partition=partition)

        db = get_vectordb()
        chunks = await db.async_search.remote(
            query=query,
            partition=partition,
            top_k=self.top_k,
            similarity_threshold=self.similarity_threshold,
        )

        extra_documents = []
        file_ids = set()
        for chunk in chunks:
            file_ids.add(chunk.metadata['file_id'])

            file_id = chunk.metadata['file_id']

            linked_documents = await db.get_related_files.remote(file_id, 'email_thread', partition, True)
            log.info(f'Got {len(linked_documents)} chunks from documents linked with {file_id}')
            for linked_file_id in set([ d.metadata['file_id'] for d in linked_documents ]):
                log.info("Linked : " + linked_file_id)

            extra_documents.extend(self.get_branch(linked_documents, chunk))

        chunks.extend(extra_documents)

        for file_id in set( [ d.metadata['file_id'] for d in chunks ] ):
            log.info(f'Found: {file_id}')

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
        "email": EmailRetriever,
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
