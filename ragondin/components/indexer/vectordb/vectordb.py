import asyncio
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union

from langchain_community.embeddings import HuggingFaceBgeEmbeddings
from langchain_core.documents.base import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_milvus import BM25BuiltInFunction, Milvus
from langchain_qdrant import FastEmbedSparse, QdrantVectorStore, RetrievalMode
from pymilvus import MilvusClient
from qdrant_client import QdrantClient, models

from .utils import PartitionFileManager
import random
import numpy as np

INDEX_PARAMS = [
    {
        "metric_type": "BM25",
        "index_type": "SPARSE_INVERTED_INDEX",
    },  # For sparse vector
    {
        "metric_type": "COSINE",
        "index_type": "HNSW",
        "params": {"M": 32, "efConstruction": 100},
    },  # For dense vector
]


class ABCVectorDB(ABC):
    """
    Abstract base class for a Vector Database.
    This class defines the interface for a vector database connector.
    """

    @abstractmethod
    async def get_collections(self):
        pass

    @abstractmethod
    async def async_add_documents(self, chunks, partition: Optional[str] = None):
        pass

    @abstractmethod
    async def async_search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: int = 0.80,
        partition: Optional[str | List[str]] = None,
        filter: Optional[Dict] = None,
    ) -> list[Document]:
        pass

    @abstractmethod
    async def async_multy_query_search(
        self, partition: list[str], queries: list[str], top_k_per_query: int = 5
    ) -> list[Document]:
        pass

    @abstractmethod
    def get_file_points(
        self, file_id: dict, partition: Optional[str] = None, limit: int = 100
    ):
        pass

    @abstractmethod
    def delete_file_points(self, points: list, file_id: str, partition: str):
        pass

    @abstractmethod
    def file_exists(self, file_id: str, partition: Optional[str] = None):
        pass

    @abstractmethod
    def collection_exists(self, collection_name: str):
        pass

    @abstractmethod
    def sample_chunk_ids(
        self, partition: str, n_ids: int = 100, seed: int | None = None
    ):
        pass

    @abstractmethod
    def list_all_chunk(
        self, partition: str, include_embedding: bool = True
    ) -> List[Document]:
        pass

    @abstractmethod
    def get_partition(self, partition: str):
        pass

    @abstractmethod
    def list_partitions(self, **kwargs):
        pass


class MilvusDB(ABCVectorDB):
    """
    MilvusDB is a concrete class to interact with a Milvus database for vector storage and retrieval.
    Attributes:
        logger: Logger instance for logging information.
        embeddings: Embeddings to be used for vector storage.
        port: Port number of the Milvus server.
        host: Host address of the Milvus server.
        client: Milvus client instance.
        sparse_embeddings: Sparse embeddings for hybrid mode.
        index_params: Parameters for indexing.
        default_collection_name: Default collection name.
        _collection_name: Internal collection name.
        vector_store: Vector store instance.
    Methods:
        collection_name: Property to get and set the collection name.
        get_collections: Asynchronously get a list of collections.
        async_search: Asynchronously search for documents based on a query.
        async_multy_query_search: Asynchronously search for documents based on multiple queries.
        async_add_documents: Asynchronously add documents to the collection.
        get_file_points: Get points associated with a file from Milvus.
        file_exists: Check if a file exists in the collection.
        collection_exists: Check if a collection exists in Milvus.
        delete_points: Delete points from Milvus.
        uri: URI of the Milvus server.
    """

    def __init__(
        self,
        host,
        port,
        embeddings: HuggingFaceBgeEmbeddings | HuggingFaceEmbeddings = None,
        collection_name: str = None,
        logger=None,
        hybrid_mode=True,
        **kwargs,
    ):
        """
        Initialize Milvus.

        Args:
            host (str): The host of the Milvus server.
            port (int): The port of the Milvus server.
            collection_name (str): The name of the collection in the Milvus database.
            embeddings (list): The embeddings.
        """

        self.logger = logger
        self.embeddings: Union[HuggingFaceBgeEmbeddings, HuggingFaceEmbeddings] = (
            embeddings
        )
        self.port = port
        self.host = host
        self.uri = f"http://{host}:{port}"
        self.client = MilvusClient(uri=self.uri)
        self.sparse_embeddings = None
        if hybrid_mode:
            self.sparse_embeddings = BM25BuiltInFunction(enable_match=True)

        index_params = None
        if hybrid_mode:
            index_params = INDEX_PARAMS
        else:
            index_params = {"metric_type": "COSINE", "index_type": "FLAT"}

        self.hybrid_mode = hybrid_mode
        self.index_params = index_params
        # Initialize collection-related attributes
        self.default_collection_name = None
        self._collection_name = None
        self.vector_store = None
        self.partition_file_manager: PartitionFileManager = None
        self.default_partition = "_default"
        self.db_dir = kwargs.get("db_dir", None)

        # Set the initial collection name (if provided)
        if collection_name:
            self.default_collection_name = collection_name
            self.collection_name = collection_name

    @property
    def collection_name(self):
        return self._collection_name

    @collection_name.setter
    def collection_name(self, name: str):
        if not name:
            if self.default_collection_name is None:
                raise ValueError("Collection name cannot be empty.")
            name = self.default_collection_name

        self.vector_store = Milvus(
            connection_args={"uri": self.uri},
            collection_name=name,
            embedding_function=self.embeddings,
            auto_id=True,
            index_params=self.index_params,
            primary_field="_id",
            enable_dynamic_field=True,
            partition_key_field="partition",
            builtin_function=self.sparse_embeddings,
            vector_field=["sparse", "vector"] if self.hybrid_mode else None,
            consistency_level="Strong",
        )

        self.partition_file_manager = PartitionFileManager(
            database_url=f"sqlite:///{self.db_dir}/partitions_for_collection_{name}.db",
            logger=self.logger,
        )

        self.logger = self.logger.bind(collection=name)
        self.logger.info("Milvus collection loaded.")

        if self.default_collection_name is None:
            self.default_collection_name = name
            self.logger.info(f"Default collection name set to `{name}`.")
        self._collection_name = name

    async def get_collections(self) -> list[str]:
        return self.client.list_collections()

    async def async_search(
        self,
        query: str,
        partition: list[str],
        top_k: int = 5,
        similarity_threshold: int = 0.80,
        filter: Optional[dict] = {},
    ) -> list[Document]:
        """
        Perform an asynchronous search on the vector store with a given query.

        Args:
            query (str): The search query string.
            top_k (int, optional): The number of top results to return. Defaults to 5.
            similarity_threshold (int, optional): The minimum similarity score threshold for results. Defaults to 0.80.
            collection_name (Optional[str], optional): The name of the collection to search in. If None, the default collection name is used. Defaults to None.

        Returns:
            list[Document]: A list of documents that match the search query.

        Raises:
            ValueError: If no collection name is provided and no default collection name is set.
            ValueError: If the specified collection does not exist.
        """

        expr_parts = []

        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")

        for key, value in filter.items():
            expr_parts.append(f"{key} == '{value}'")

        # Join all parts with " and " only if there are multiple conditions
        expr = " and ".join(expr_parts) if expr_parts else ""

        SEARCH_PARAMS = [
            {
                "metric_type": "COSINE",
                "params": {
                    "ef": 20,
                    "radius": similarity_threshold,
                    "range_filter": 1.0,
                },
            },
            {"metric_type": "BM25", "params": {"drop_ratio_build": 0.2}},
        ]

        # "params": {"drop_ratio_build": 0.2, "bm25_k1": 1.2, "bm25_b": 0.75},

        if self.hybrid_mode:
            docs_scores = await self.vector_store.asimilarity_search_with_score(
                query=query,
                k=top_k,
                fetch_k=top_k,
                ranker_type="rrf",
                ranker_params={"k": 100},
                expr=expr,
                param=SEARCH_PARAMS,
            )
        else:
            docs_scores = (
                await self.vector_store.asimilarity_search_with_relevance_scores(
                    query=query,
                    k=top_k,
                    score_threshold=similarity_threshold,
                    expr=expr,
                )
            )

        for doc, score in docs_scores:
            doc.metadata["score"] = score

        docs = [doc for doc, score in docs_scores]

        return docs

    async def async_multy_query_search(
        self,
        partition: list[str],
        queries: list[str],
        top_k_per_query: int = 5,
        similarity_threshold: int = 0.80,
    ) -> list[Document]:
        """
        Perform multiple asynchronous search queries concurrently and return the results.
        Args:
            queries (list[str]): A list of search query strings.
            top_k_per_query (int, optional): The number of top results to return per query. Defaults to 5.
            similarity_threshold (int, optional): The similarity threshold for filtering results. Defaults to 0.80.
            collection_name (Optional[str], optional): The name of the collection to search within. Defaults to None.
        Returns:
            list[Document]: A list of unique documents retrieved from the search queries.
        """
        # Gather all search tasks concurrently
        search_tasks = [
            self.async_search(
                query=query,
                partition=partition,
                top_k=top_k_per_query,
                similarity_threshold=similarity_threshold,
            )
            for query in queries
        ]
        retrieved_results = await asyncio.gather(*search_tasks)
        # Process the retrieved documents
        retrieved_chunks = {}
        for retrieved in retrieved_results:
            if retrieved:
                for document in retrieved:
                    retrieved_chunks[document.metadata["_id"]] = document

        retrieved_chunks = list(retrieved_chunks.values())
        retrieved_chunks.sort(key=lambda v: v.metadata["score"], reverse=True)

        return retrieved_chunks

    async def async_add_documents(self, chunks: list[Document]) -> None:
        """Asynchronously add documents to the vector store."""

        try:
            file_metadata = dict(chunks[0].metadata)
            file_metadata.pop("page")
            file_id, partition = (
                file_metadata.get("file_id"),
                file_metadata.get("partition"),
            )

            # check if this file_id exists
            res = self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            )
            if res:
                raise ValueError(
                    f"No Insertion: This File ({file_id}) already exists in Partition ({partition})"
                )

            # Insert Chunks
            await self.vector_store.aadd_documents(chunks)

            # insert file_id and partition into partition_file_manager
            self.partition_file_manager.add_file_to_partition(
                file_id=file_id, partition=partition, file_metadata=file_metadata
            )
        except Exception as e:
            self.logger.exception(
                "Error while adding documents to Milvus", error=str(e)
            )
            raise e

    def get_file_points(self, file_id: str, partition: str, limit: int = 100):
        """
        Retrieve file points from the vector database based on a filter.
        Args:
            filter (dict): A dictionary containing the filter key and value.
            collection_name (Optional[str], optional): The name of the collection to query. Defaults to None.
            limit (int, optional): The maximum number of results to return per query. Defaults to 100.
        Returns:
            list: A list of result IDs that match the filter criteria.
        Raises:
            ValueError: If the filter value type is unsupported.
            Exception: If there is an error during the query process.
        """
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            if not self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            ):
                return []

            # Adjust filter expression based on the type of value
            filter_expression = f"partition == '{partition}' and file_id == '{file_id}'"

            # Pagination parameters
            offset = 0
            results = []

            while True:
                response = self.client.query(
                    collection_name=self.collection_name,
                    filter=filter_expression,
                    output_fields=["_id"],  # Only fetch IDs
                    limit=limit,
                    offset=offset,
                )

                if not response:
                    break  # No more results

                results.extend([res["_id"] for res in response])
                offset += len(response)  # Move offset forward

                if limit == 1:
                    return [response[0]["_id"]] if response else []
            log.info("Fetched file points.", count=len(results))
            return results

        except Exception:
            log.exception(f"Couldn't fetch file points for file_id {file_id}")
            raise

    def get_file_chunks(
        self, file_id: str, partition: str, include_id: bool = False, limit: int = 100
    ):
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            if not self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            ):
                return []

            # Adjust filter expression based on the type of value
            filter_expression = f"partition == '{partition}' and file_id == '{file_id}'"

            # Pagination parameters
            offset = 0
            results = []
            excluded_keys = (
                ["text", "vector", "_id"] if not include_id else ["text", "vector"]
            )

            while True:
                response = self.client.query(
                    collection_name=self.collection_name,
                    filter=filter_expression,
                    limit=limit,
                    offset=offset,
                )

                if not response:
                    break  # No more results

                results.extend(response)
                offset += len(response)  # Move offset forward

            docs = [
                Document(
                    page_content=res["text"],
                    metadata={
                        key: value
                        for key, value in res.items()
                        if key not in excluded_keys
                    },
                )
                for res in results
            ]
            log.info("Fetched file chunks.", count=len(results))
            return docs

        except Exception:
            log.exception(f"Couldn't get file chunks for file_id {file_id}")
            raise

    def get_chunk_by_id(self, chunk_id: str):
        """
        Retrieve a chunk by its ID.
        Args:
            chunk_id (str): The ID of the chunk to retrieve.
        Returns:
            Document: The retrieved chunk.
        """
        try:
            response = self.client.query(
                collection_name=self.collection_name,
                filter=f"_id == {chunk_id}",
                limit=1,
            )
            if response:
                return Document(
                    page_content=response[0]["text"],
                    metadata={
                        key: value
                        for key, value in response[0].items()
                        if key not in ["text", "vector"]
                    },
                )
            return None
        except Exception:
            self.logger.exception("Couldn't get chunk by ID", chunk_id=chunk_id)

    def delete_file_points(self, points: list, file_id: str, partition: str):
        """
        Delete points from Milvus
        """
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            if not self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            ):
                raise ValueError(
                    f"This File ({file_id}) doesn't exist in Partition ({partition})"
                )

            self.client.delete(collection_name=self.collection_name, ids=points)
            self.partition_file_manager.remove_file_from_partition(
                file_id=file_id, partition=partition
            )
            log.info("File points deleted.")
        except Exception:
            log.exception("Error while deleting file points.")

    def file_exists(self, file_id: str, partition: str):
        """
        Check if a file exists in Milvus
        """
        try:
            return self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            )
        except Exception:
            self.logger.exception(
                "File existence check failed.", file_id=file_id, partition=partition
            )
            return False

    def get_partition(self, partition: str):
        try:
            partition_dict = self.partition_file_manager.get_partition(
                partition=partition
            )
            return partition_dict

        except Exception:
            self.logger.exception("Failed get this partition.", partition=partition)
            raise

    def list_partitions(self, **kwargs):
        try:
            return self.partition_file_manager.list_partitions(**kwargs)
        except Exception as e:
            self.logger.exception(f"Failed to list partitions: {e}")
            raise

    def collection_exists(self, collection_name: str):
        """
        Check if a collection exists in Milvus
        """
        return self.vector_store.client.has_collection(collection_name)

    def delete_partition(self, partition: str):
        log = self.logger.bind(partition=partition)
        if not self.partition_file_manager.partition_exists(partition):
            log.debug(f"Partition {partition} does not exist")
            return False

        try:
            count = self.client.delete(
                collection_name=self.collection_name,
                filter=f"partition == '{partition}'",
            )

            self.partition_file_manager.delete_partition(partition)

            log.info("Deleted points from partition", count=count.get("delete_count"))

            return True
        except Exception:
            log.exception("Failed to delete partition")
            return False

    def partition_exists(self, partition: str):
        """
        Check if a partition exists in Milvus
        """
        try:
            return self.partition_file_manager.partition_exists(partition=partition)

        except Exception:
            self.logger.exception(
                "Partition existence check failed.", partition=partition
            )
            return False

    def sample_chunk_ids(
        self, partition: str, n_ids: int = 100, seed: int | None = None
    ):
        """
        Sample chunk IDs from a given partition."""

        try:
            if not self.partition_file_manager.partition_exists(partition):
                return []

            file_ids = self.partition_file_manager.sample_file_ids(
                partition=partition, n_file_id=10_000
            )
            if not file_ids:
                return []

            # Create a filter expression for the query
            filter_expression = f"partition == '{partition}' and file_id in {file_ids}"

            ids = []
            iterator = self.client.query_iterator(
                collection_name=self.collection_name,
                filter=filter_expression,
                batch_size=16000,
                output_fields=["_id"],
            )

            ids = []
            while True:
                result = iterator.next()
                if not result:
                    iterator.close()
                    break

                ids.extend([res["_id"] for res in result])

            random.seed(seed)
            sampled_ids = random.sample(ids, min(n_ids, len(ids)))
            return sampled_ids

        except Exception as e:
            self.logger.exception(
                f"Error in `sample_chunks` for partition {partition}: {e}"
            )
            raise e

    def list_all_chunk(self, partition: str, include_embedding: bool = True):
        """
        List all chunk from a given partition.
        """
        try:
            if not self.partition_file_manager.partition_exists(partition):
                return []

            # Create a filter expression for the query
            filter_expression = f"partition == '{partition}'"

            excluded_keys = ["text"]
            if not include_embedding:
                excluded_keys.append("vector")

            def prepare_metadata(res: dict):
                metadata = {}
                for k, v in res.items():
                    if not k in excluded_keys:
                        if k == "vector":
                            v = str(np.array(v).flatten().tolist())
                        metadata[k] = v
                return metadata

            chunks = []
            iterator = self.client.query_iterator(
                collection_name=self.collection_name,
                filter=filter_expression,
                batch_size=16000,
                output_fields=["*"],
            )

            while True:
                result = iterator.next()
                if not result:
                    iterator.close()
                    break
                chunks.extend(
                    [
                        Document(
                            page_content=res["text"],
                            metadata=prepare_metadata(res),
                        )
                        for res in result
                    ]
                )

            return chunks

        except Exception as e:
            self.logger.exception(
                f"Error in `list_chunk_ids` for partition {partition}: {e}"
            )
            raise


class QdrantDB(ABCVectorDB):
    """
    QdrantDB is a class that provides an interface to interact with a Qdrant vector database. It allows for the initialization of a Qdrant client, setting and getting collection names, performing asynchronous searches, adding documents, retrieving file points, deleting points, and checking for the existence of files and collections.
    Attributes:
        logger: Logger instance for logging information.
        embeddings: Embeddings used for vector storage.
        port: Port number of the Qdrant server.
        host: Host address of the Qdrant server.
        client: Qdrant client instance.
        sparse_embeddings: Sparse embeddings for hybrid retrieval mode.
        retrieval_mode: Mode of retrieval (HYBRID or DENSE).
        default_collection_name: Default collection name.
        _collection_name: Current collection name.
        vector_store: Instance of QdrantVectorStore.
    Methods:
        __init__(host, port, embeddings, collection_name, logger, hybrid_mode):
            Initializes the QdrantDB instance with the given parameters.
        collection_name:
            Property to get and set the collection name.
        get_collections() -> list[str]:
            Asynchronously retrieves a list of collection names from the Qdrant database.
        async_search(query, top_k, similarity_threshold, collection_name) -> list[Document]:
            Asynchronously searches for documents in the specified collection based on the query.
        async_multy_query_search(queries, top_k_per_query, similarity_threshold, collection_name) -> list[Document]:
            Asynchronously performs multiple queries and retrieves documents for each query.
        async_add_documents(chunks, collection_name) -> None:
            Asynchronously adds documents to the specified collection.
        get_file_points(filter, collection_name, limit) -> list:
            Retrieves points associated with a file from the Qdrant database based on the filter.
        delete_points(points, collection_name) -> None:
            Deletes points from the specified collection in the Qdrant database.
        file_exists(file_name, collection_name) -> bool:
            Checks if a file exists in the specified collection in the Qdrant database.
        collection_exists(collection_name) -> bool:
            Checks if a collection exists in the Qdrant database.
    """

    def __init__(
        self,
        host,
        port,
        embeddings: HuggingFaceBgeEmbeddings | HuggingFaceEmbeddings = None,
        collection_name: str = None,
        logger=None,
        hybrid_mode=True,
        **kwargs,
    ):
        """
        Initialize Qdrant_Connector.

        Args:
            host (str): The host of the Qdrant server.
            port (int): The port of the Qdrant server.
            collection_name (str): The name of the collection in the Qdrant database.
            embeddings (list): The embeddings.
        """

        self.logger = logger
        self.embeddings: Union[HuggingFaceBgeEmbeddings, HuggingFaceEmbeddings] = (
            embeddings
        )
        self.port = port
        self.host = host
        self.client = QdrantClient(
            port=port,
            host=host,
            prefer_grpc=False,
        )

        self.sparse_embeddings = (
            FastEmbedSparse(model_name="Qdrant/bm25") if hybrid_mode else None
        )
        self.retrieval_mode = (
            RetrievalMode.HYBRID if hybrid_mode else RetrievalMode.DENSE
        )
        logger.info(f"VectorDB retrieval mode: {self.retrieval_mode}")

        # Initialize collection-related attributes
        self.default_collection_name = None
        self._collection_name = None
        self.vector_store = None

        # Set the initial collection name (if provided)
        if collection_name:
            self.default_collection_name = collection_name
            self.collection_name = collection_name

    @property
    def collection_name(self):
        return self._collection_name

    @collection_name.setter
    def collection_name(self, name: str):
        if not name:
            if self.default_collection_name is None:
                raise ValueError("Collection name cannot be empty.")
            name = self.default_collection_name

        if self.client.collection_exists(collection_name=name):
            self.vector_store = QdrantVectorStore(
                client=self.client,
                collection_name=name,
                embedding=self.embeddings,
                sparse_embedding=self.sparse_embeddings,
                retrieval_mode=self.retrieval_mode,
            )
            self.logger.warning(f"Collection `{name}` LOADED.")
        else:
            self.vector_store = QdrantVectorStore.construct_instance(
                embedding=self.embeddings,
                sparse_embedding=self.sparse_embeddings,
                collection_name=name,
                client_options={"port": self.port, "host": self.host},
                retrieval_mode=self.retrieval_mode,
            )
            self.logger.debug(f"Collection `{name}` CREATED.")

        if self.default_collection_name is None:
            self.default_collection_name = name
            self.logger.info(f"Default collection name set to `{name}`.")
        self._collection_name = name

    async def get_collections(self) -> list[str]:
        return [c.name for c in self.client.get_collections().collections]

    async def async_search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: int = 0.80,
        collection_name: Optional[str] = None,
    ) -> list[Document]:
        """
        Perform an asynchronous search on the vector database.

        Args:
            query (str): The search query string.
            top_k (int, optional): The number of top results to return. Defaults to 5.
            similarity_threshold (int, optional): The minimum similarity score threshold for results. Defaults to 0.80.
            collection_name (Optional[str], optional): The name of the collection to search in. If None, the default collection name is used. Defaults to None.

        Returns:
            list[Document]: A list of documents that match the search query.

        Raises:
            ValueError: If no collection name is provided and no default collection name is set.
            ValueError: If the specified collection does not exist.
        """
        if collection_name is None:
            if self.default_collection_name is None:
                raise ValueError(
                    "Collection name not provided and no default collection name set."
                )
            self.collection_name = self.default_collection_name
        elif not self.collection_exists(collection_name):
            raise ValueError(f"Collection {collection_name} does not exist.")
        else:
            self.collection_name = collection_name
        docs_scores = await self.vector_store.asimilarity_search_with_relevance_scores(
            query=query, k=top_k, score_threshold=similarity_threshold
        )
        docs = [doc for doc, score in docs_scores]
        return docs

    async def async_multy_query_search(
        self,
        queries: list[str],
        top_k_per_query: int = 5,
        similarity_threshold: int = 0.80,
        collection_name: Optional[str] = None,
    ) -> list[Document]:
        """
        Perform multiple asynchronous search queries concurrently and return the unique retrieved documents.

        Args:
            queries (list[str]): A list of search query strings.
            top_k_per_query (int, optional): The number of top results to retrieve per query. Defaults to 5.
            similarity_threshold (int, optional): The similarity threshold for filtering results. Defaults to 0.80.
            collection_name (Optional[str], optional): The name of the collection to search within. Defaults to None.

        Returns:
            list[Document]: A list of unique retrieved documents.
        """
        # Set the collection name
        self.collection_name = collection_name
        # Gather all search tasks concurrently
        search_tasks = [
            self.async_search(
                query=query,
                top_k=top_k_per_query,
                similarity_threshold=similarity_threshold,
                collection_name=collection_name,
            )
            for query in queries
        ]
        retrieved_results = await asyncio.gather(*search_tasks)
        retrieved_chunks = {}
        # Process the retrieved documents
        for retrieved in retrieved_results:
            if retrieved:
                for document in retrieved:
                    retrieved_chunks[document.metadata["_id"]] = document
        return list(retrieved_chunks.values())

    async def async_add_documents(
        self, chunks: list[Document], collection_name: Optional[str] = None
    ) -> None:
        """
        Asynchronously add documents to the vector store.
        Args:
            chunks (list[Document]): A list of Document objects to be added.
            collection_name (Optional[str]): The name of the collection to which the documents will be added.
                                             If None, the default collection name will be used.
        Returns:
            None
        """
        # Set the collection name
        self.collection_name = collection_name

        await self.vector_store.aadd_documents(chunks)
        self.logger.debug("CHUNKS INSERTED")

    def get_file_points(
        self, filter: dict, collection_name: Optional[str] = None, limit: int = 100
    ):
        """
        Retrieve file points from the vector database based on a filter.
        Args:
            filter (dict): A dictionary containing the filter key and value.
            collection_name (Optional[str], optional): The name of the collection to search in. Defaults to None.
            limit (int, optional): The maximum number of results to return. Defaults to 100.
        Returns:
            List[str]: A list of result IDs that match the filter criteria.
        Raises:
            Exception: If there is an error during the retrieval process.
        """

        try:
            # Scroll through all vectors
            has_more = True
            offset = None
            results = []

            key = next(iter(filter))
            value = filter[key]

            while has_more:
                response = self.client.scroll(
                    collection_name=collection_name
                    if collection_name
                    else self.default_collection_name,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key=f"metadata.{key}",
                                match=models.MatchValue(value=value),
                            )
                        ]
                    ),
                    limit=limit,
                    offset=offset,
                )

                # Add points that contain the filename in metadata.source
                results.extend(response[0])
                has_more = response[1]  # Check if there are more results
                offset = response[1] if has_more else None

                if limit == 1:
                    return [results[0].id] if results else []

            # Return list of result ids
            return [res.id for res in results]

        except Exception as e:
            self.logger.error(f"Couldn't get file points for {key} {value}: {e}")
            raise

    def delete_file_points(self, points: list, collection_name: Optional[str] = None):
        """
        Delete points from Qdrant
        """
        try:
            self.client.delete(
                collection_name=collection_name
                if collection_name
                else self.default_collection_name,
                points_selector=models.PointIdsList(points=points),
            )
        except Exception as e:
            self.logger.error(f"Error in `delete_points`: {e}")

    def file_exists(self, file_name: str, collection_name: Optional[str] = None):
        """
        Check if a file exists in Qdrant
        """
        try:
            # Get points associated with the file name
            points = self.get_file_points(
                {"file_name": file_name}, collection_name, limit=1
            )
            return True if points else False
        except Exception as e:
            self.logger.error(f"Error in `file_exists` for {file_name}: {e}")
            return False

    def collection_exists(self, collection_name: str):
        """
        Check if a collection exists in Qdrant
        """
        return self.client.collection_exists(collection_name)


class ConnectorFactory:
    CONNECTORS = {"milvus": MilvusDB, "qdrant": QdrantDB}

    @staticmethod
    def create_vdb(config, logger, embeddings) -> ABCVectorDB:
        if not config["vectordb"]["enable"]:
            logger.info("Vector database is not enabled. Skipping initialization.")
            return None

        # Extract parameters
        dbconfig = dict(config.vectordb)
        name = dbconfig.pop("connector_name")
        dbconfig.pop("enable")
        vdb_cls = ConnectorFactory.CONNECTORS.get(name)
        if not vdb_cls:
            raise ValueError(f"VECTORDB '{name}' is not supported.")

        dbconfig["embeddings"] = embeddings
        dbconfig["logger"] = logger
        dbconfig["db_dir"] = config.paths.db_dir

        return vdb_cls(**dbconfig)
