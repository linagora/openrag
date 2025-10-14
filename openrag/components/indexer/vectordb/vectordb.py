import asyncio
from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np
import ray
from config import load_config
from langchain_core.documents.base import Document
from pymilvus import (
    AnnSearchRequest,
    AsyncMilvusClient,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    MilvusException,
    RRFRanker,
)
from utils.exceptions.base import EmbeddingError
from utils.exceptions.vectordb import *
from utils.logger import get_logger

from ..embeddings import OpenAIEmbedding
from .utils import PartitionFileManager

logger = get_logger()
config = load_config()


class BaseVectorDB(ABC):
    """
    Abstract base class for a Vector Database.
    This class defines the interface for a vector database connector.
    """

    @abstractmethod
    async def list_collections(self):
        pass

    @abstractmethod
    def collection_exists(self, collection_name: str):
        pass

    @abstractmethod
    def list_partitions(self):
        pass

    @abstractmethod
    def partition_exists(self, partition: str) -> bool:
        pass

    @abstractmethod
    async def delete_partition(self, partition: str):
        pass

    @abstractmethod
    def list_partition_files(self, partition: str, limit: Optional[int] = None):
        pass

    @abstractmethod
    async def delete_file(self, file_id: str, partition: str):
        pass

    @abstractmethod
    async def async_add_documents(self, chunks: list[Document]):
        pass

    @abstractmethod
    async def async_search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: int = 0.60,
        partition: list[str] = None,
        filter: Optional[dict] = None,
    ) -> list[Document]:
        pass

    @abstractmethod
    async def async_multi_query_search(
        self,
        partition: list[str],
        queries: list[str],
        top_k_per_query: int = 5,
        similarity_threshold: int = 0.6,
        filter: Optional[dict] = None,
    ) -> list[Document]:
        pass

    @abstractmethod
    async def list_all_chunk(
        self, partition: str, include_embedding: bool = True
    ) -> List[Document]:
        pass

    @abstractmethod
    async def get_file_chunks(
        self, file_id: str, partition: str, include_id: bool = False, limit: int = 100
    ):
        pass

    @abstractmethod
    async def get_chunk_by_id(self, chunk_id: str):
        pass

    # @abstractmethod
    # def sample_chunk_ids(
    #     self, partition: str, n_ids: int = 100, seed: int | None = None
    # ):
    #     pass


MAX_LENGTH = 65_535


@ray.remote
class MilvusDB(BaseVectorDB):
    def __init__(self):
        try:
            from config import load_config
            from utils.logger import get_logger

            self.config = load_config()
            self.logger = get_logger()

            # init milvus clients
            self.port = self.config.vectordb.get("port")
            self.host = self.config.vectordb.get("host")
            uri = f"http://{self.host}:{self.port}"
            self.uri = uri
            try:
                self._client = MilvusClient(uri=uri)
                self._async_client = AsyncMilvusClient(uri=uri)
            except MilvusException as e:
                raise VDBConnectionError(
                    f"Failed to connect to Milvus: {str(e)}",
                    db_url=uri,
                    db_type="Milvus",
                )

            # embedder
            self.embedder: OpenAIEmbedding = OpenAIEmbedding(
                embeddings_config=self.config.embedder
            )

            self.hybrid_search = self.config.vectordb.get("hybrid_search", True)
            # partition related params
            self.rdb_host = self.config.rdb.host
            self.rdb_port = self.config.rdb.port
            self.rdb_user = self.config.rdb.user
            self.rdb_password = self.config.rdb.password
            self.partition_file_manager: PartitionFileManager = None

            # Initialize collection-related attributes
            self.collection_name = self.config.vectordb.get(
                "collection_name", "vdb_test"
            )
            self.collection_loaded = False
            self.load_collection()

        except VDBError:
            raise

        except Exception as e:
            self.logger.exception(
                "Unexpected error initializing Milvus clients", error=str(e)
            )
            raise VDBConnectionError(
                f"Unexpected error initializing Milvus clients: {str(e)}",
                db_url=uri,
                db_type="Milvus",
            )

    def load_collection(self):
        if not self.collection_loaded:
            self.logger = self.logger.bind(
                collection=self.collection_name, database="Milvus"
            )
            try:
                if self._client.has_collection(self.collection_name):
                    self.logger.warning(
                        f"Collection `{self.collection_name}` already exists. Loading it."
                    )
                else:
                    self.logger.info("Creating empty collection")
                    index_params = self._create_index()
                    schema = self._create_schema()
                    consistency_level = "Strong"
                    try:
                        self._client.create_collection(
                            collection_name=self.collection_name,
                            schema=schema,
                            consistency_level=consistency_level,
                            index_params=index_params,
                            enable_dynamic_field=True,
                        )
                    except MilvusException as e:
                        self.logger.exception(
                            f"Failed to create collection `{self.collection_name}`",
                            error=str(e),
                        )
                        raise VDBCreateOrLoadCollectionError(
                            f"Failed to create collection `{self.collection_name}`: {str(e)}",
                            collection_name=self.collection_name,
                            operation="create_collection",
                        )
                try:
                    self._client.load_collection(self.collection_name)
                    self.collection_loaded = True
                except MilvusException as e:
                    self.logger.exception(
                        f"Failed to load collection `{self.collection_name}`",
                        error=str(e),
                    )
                    raise VDBCreateOrLoadCollectionError(
                        f"Failed to load existing collection `{self.collection_name}`: {str(e)}",
                        collection_name=self.collection_name,
                        operation="load_collection",
                    )

                self.partition_file_manager = PartitionFileManager(
                    database_url=f"postgresql://{self.rdb_user}:{self.rdb_password}@{self.rdb_host}:{self.rdb_port}/partitions_for_collection_{self.collection_name}",
                    logger=self.logger,
                )
                self.logger.info("Milvus collection loaded.")
            except VDBError:
                raise
            except Exception as e:
                self.logger.exception(
                    f"Unexpected error setting collection name `{self.collection_name}`",
                    error=str(e),
                )
                raise UnexpectedVDBError(
                    f"Unexpected error setting collection name `{self.collection_name}`: {str(e)}",
                    collection_name=self.collection_name,
                )

    def _create_schema(self):
        self.logger.info("Creating Schema")
        schema = self._client.create_schema(enable_dynamic_field=True)
        schema.add_field(
            field_name="_id", datatype=DataType.INT64, is_primary=True, auto_id=True
        )
        schema.add_field(
            field_name="text",
            datatype=DataType.VARCHAR,
            enable_analyzer=True,
            enable_match=True,
            max_length=MAX_LENGTH,
        )

        schema.add_field(
            field_name="partition",
            datatype=DataType.VARCHAR,
            max_length=MAX_LENGTH,
            is_partition_key=True,
        )

        schema.add_field(
            field_name="file_id",
            datatype=DataType.VARCHAR,
            max_length=MAX_LENGTH,
        )

        schema.add_field(
            field_name="vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self.embedder.embedding_dimension,
        )

        if self.hybrid_search:
            # Add sparse field for BM25 - this will be auto-generated
            schema.add_field(
                field_name="sparse",
                datatype=DataType.SPARSE_FLOAT_VECTOR,
                index_type="SPARSE_INVERTED_INDEX",
            )

            # BM25 function to auto-generate sparse embeddings
            bm25_function = Function(
                name="text_bm25_emb",
                function_type=FunctionType.BM25,
                input_field_names=["text"],
                output_field_names=["sparse"],
            )

            # Add the function to our schema
            schema.add_function(bm25_function)
        return schema

    def _create_index(self):
        self.logger.info("Creating Index")
        index_params = self._client.prepare_index_params()
        # Add index for file_id field
        index_params.add_index(
            field_name="file_id",
            index_type="INVERTED",
            index_name="file_id_idx",
        )

        # ADD index for partition field
        index_params.add_index(
            field_name="partition", index_type="INVERTED", index_name="partition_idx"
        )

        # Add index for vector field
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE",
            index_params={"M": 128, "efConstruction": 256, "metric_type": "COSINE"},
        )

        # Add index for sparase field
        index_params.add_index(
            field_name="sparse",
            index_name="sparse_idx",
            index_type="SPARSE_INVERTED_INDEX",
            index_params={
                "metric_type": "BM25",
                "inverted_index_algo": "DAAT_MAXSCORE",
                "bm25_k1": 1.2,
                "bm25_b": 0.75,
            },
        )
        return index_params

    async def list_collections(self) -> list[str]:
        return self._client.list_collections()

    async def async_add_documents(self, chunks: list[Document]) -> None:
        """Asynchronously add documents to the vector store."""

        try:
            file_metadata = dict(chunks[0].metadata)
            file_metadata.pop("page")
            file_id, partition = (
                file_metadata.get("file_id"),
                file_metadata.get("partition"),
            )
            self.logger.bind(
                partition=partition,
                file_id=file_id,
                filename=file_metadata.get("filename"),
            )

            # check if this file_id exists
            res = self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            )
            if res:
                error_msg = f"This File Id ({file_id}) already exists in Partition ({partition})"
                self.logger.error(error_msg)
                raise VDBInsertError(
                    error_msg,
                    status_code=409,
                    collection_name=self.collection_name,
                    partition=partition,
                    file_id=file_id,
                )

            entities = await self.embedder.embed_documents(chunks)
            await self._async_client.insert(
                collection_name=self.collection_name,
                data=entities,
            )

            # insert file_id and partition into partition_file_manager
            self.partition_file_manager.add_file_to_partition(
                file_id=file_id, partition=partition, file_metadata=file_metadata
            )
            self.logger.info(f"File '{file_id}' added to partition '{partition}'")
        except EmbeddingError as e:
            self.logger.exception("Embedding failed", error=str(e))
            raise
        except VDBError as e:
            self.logger.exception("VectorDB operation failed", error=str(e))
            raise

        except Exception as e:
            self.logger.exception(
                "Unexpected error while adding a document", error=str(e)
            )
            raise UnexpectedVDBError(
                f"Unexpected error while adding a document: {str(e)}",
                collection_name=self.collection_name,
            )

    async def async_multi_query_search(
        self,
        partition,
        queries,
        top_k_per_query=5,
        similarity_threshold=0.6,
        filter=None,
    ) -> list[Document]:
        # Gather all search tasks concurrently
        search_tasks = [
            self.async_search(
                query=query,
                top_k=top_k_per_query,
                similarity_threshold=similarity_threshold,
                partition=partition,
                filter=filter,
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

    async def async_search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: int = 0.80,
        partition: list[str] = None,
        filter: Optional[dict] = None,
    ) -> list[Document]:
        expr_parts = []
        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")

        if filter:
            for key, value in filter.items():
                expr_parts.append(f"{key} == '{value}'")

        # Join all parts with " and " only if there are multiple conditions
        expr = " and ".join(expr_parts) if expr_parts else ""

        try:
            query_vector = await self.embedder.embed_query(query)
            vector_param = {
                "data": [query_vector],
                "anns_field": "vector",
                "param": {
                    "metric_type": "COSINE",
                    "params": {
                        "ef": 64,
                        "radius": similarity_threshold,
                        "range_filter": 1.0,
                    },
                },
                "limit": top_k,
                "expr": expr,
            }
            if self.hybrid_search:
                sparse_param = {
                    "data": [query],
                    "anns_field": "sparse",
                    "param": {
                        "metric_type": "BM25",
                        "params": {"drop_ratio_build": 0.2},
                    },
                    "limit": top_k,
                    "expr": expr,
                }
                reqs = [
                    AnnSearchRequest(**vector_param),
                    AnnSearchRequest(**sparse_param),
                ]
                response = await self._async_client.hybrid_search(
                    collection_name=self.collection_name,
                    reqs=reqs,
                    ranker=RRFRanker(100),
                    output_fields=["*"],
                    limit=top_k,
                )
            else:
                response = await self._async_client.search(
                    collection_name=self.collection_name,
                    output_fields=["*"],
                    limit=top_k,
                    **vector_param,
                )

            return _parse_documents_from_search_results(response)
        except MilvusException as e:
            self.logger.exception("Search failed in Milvus", error=str(e))
            raise VDBSearchError(
                f"Search failed in Milvus: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except EmbeddingError as e:
            self.logger.exception(
                "Embedding failed while processing the query", error=str(e)
            )
            raise

        except Exception as e:
            self.logger.exception("Unexpected error occurred", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error occurred: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )

    async def delete_file(self, file_id: str, partition: str):
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            if not self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            ):
                # raise VDB
                log.exception(
                    f"File ID {file_id} does not exist in partition {partition}"
                )
                raise VDBFileNotFoundError(
                    f"File ID '{file_id}' does not exist in partition {partition}",
                    collection_name=self.collection_name,
                    partition=partition,
                    file_id=file_id,
                )
            res = await self._async_client.delete(
                collection_name=self.collection_name,
                filter=f"partition == '{partition}' and file_id == '{file_id}'",
            )

            self.partition_file_manager.remove_file_from_partition(
                file_id=file_id, partition=partition
            )
            log.info(
                "Deleted file chunks from partition.", count=res.get("delete_count", 0)
            )

        except MilvusException as e:
            log.exception(
                f"Couldn't delete file chunks for file_id {file_id}", error=str(e)
            )
            raise VDBDeleteError(
                f"Couldn't delete file chunks for file_id {file_id}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except VDBError:
            raise
        except Exception as e:
            log.exception("Unexpected error while deleting file chunks", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting file chunks {file_id}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    async def get_file_chunks(
        self, file_id: str, partition: str, include_id: bool = False, limit: int = 100
    ):
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            if not self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            ):
                log.exception(
                    f"File ID '{file_id}' does not exist in partition '{partition}'"
                )
                raise VDBFileNotFoundError(
                    f"File ID '{file_id}' does not exist in partition '{partition}'",
                    collection_name=self.collection_name,
                    partition=partition,
                    file_id=file_id,
                )

            # Adjust filter expression based on the type of value
            filter_expression = "partition == {partition} and file_id == {file_id}"
            filter_params = {"partition": partition, "file_id": file_id}

            # Pagination parameters
            offset = 0
            results = []
            excluded_keys = (
                ["text", "vector", "_id"] if not include_id else ["text", "vector"]
            )

            while True:
                response = await self._async_client.query(
                    collection_name=self.collection_name,
                    filter=filter_expression,
                    filter_params=filter_params,
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

        except MilvusException as e:
            log.exception(
                f"Couldn't get file chunks for file_id {file_id}", error=str(e)
            )
            raise VDBSearchError(
                f"Couldn't get file chunks for file_id {file_id}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except VDBError:
            raise

        except Exception as e:
            log.exception("Unexpected error while getting file chunks", error=str(e))
            raise VDBSearchError(
                f"Unexpected error while getting file chunks {file_id}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    async def get_chunk_by_id(self, chunk_id: str):
        """
        Retrieve a chunk by its ID.
        Args:
            chunk_id (str): The ID of the chunk to retrieve.
        Returns:
            Document: The retrieved chunk.
        """
        log = self.logger.bind(chunk_id=chunk_id)
        try:
            response = await self._async_client.query(
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
        except MilvusException as e:
            log.exception("Milvus query failed", error=str(e))
            raise VDBSearchError(
                f"Milvus query failed: {str(e)}",
                collection_name=self.collection_name,
            )

        except Exception as e:
            log.exception("Unexpected error while retrieving chunk", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while retrieving chunk {chunk_id}: {str(e)}",
                collection_name=self.collection_name,
            )

    def file_exists(self, file_id: str, partition: str):
        """
        Check if a file exists in Milvus
        """
        try:
            return self.partition_file_manager.file_exists_in_partition(
                file_id=file_id, partition=partition
            )
        except Exception as e:
            self.logger.exception(
                "File existence check failed.",
                file_id=file_id,
                partition=partition,
                error=str(e),
            )
            return False

    def list_partition_files(self, partition: str, limit: Optional[int] = None):
        try:
            partition_dict = self.partition_file_manager.list_partition_files(
                partition=partition, limit=limit
            )
            if not partition_dict:
                self.logger.warning(
                    f"Partition does exist or No files found in partition {partition}"
                )
                raise VDBPartitionNotFound(
                    f"Partition `{partition}` does not exist or no files found.",
                    collection_name=self.collection_name,
                    partition=partition,
                )

            return partition_dict

        except MilvusException as e:
            self.logger.exception(
                f"Failed to list files in partition {partition}", error=str(e)
            )
            raise VDBPartitionNotFound(
                f"Failed to list files in partition `{partition}`: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except VDBError:
            raise

        except Exception as e:
            self.logger.exception(
                f"Unexpected error while listing files in partition {partition}",
                error=str(e),
            )
            raise UnexpectedVDBError(
                f"Unexpected error while listing files in partition {partition}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )

    def list_partitions(self):
        try:
            return self.partition_file_manager.list_partitions()
        except Exception as e:
            self.logger.exception("Failed to list partitions", error=str(e))
            raise

    def collection_exists(self, collection_name: str):
        """
        Check if a collection exists in Milvus
        """
        return self._client.has_collection(collection_name=collection_name)

    async def delete_partition(self, partition: str):
        log = self.logger.bind(partition=partition)
        if not self.partition_file_manager.partition_exists(partition):
            log.debug(f"Partition {partition} does not exist")
            return False

        try:
            count = self._client.delete(
                collection_name=self.collection_name,
                filter=f"partition == '{partition}'",
            )

            self.partition_file_manager.delete_partition(partition)
            log.info("Deleted points from partition", count=count.get("delete_count"))

        except MilvusException as e:
            log.exception("Failed to delete partition", error=str(e))
            raise VDBDeleteError(
                f"Failed to delete partition `{partition}`: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except VDBError as e:
            log.exception("VectorDB operation failed", error=str(e))
            raise e
        except Exception as e:
            log.exception("Unexpected error while deleting partition", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting partition {partition}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )

    def partition_exists(self, partition: str):
        """
        Check if a partition exists in Milvus
        """
        log = self.logger.bind(partition=partition)
        try:
            return self.partition_file_manager.partition_exists(partition=partition)
        except Exception as e:
            log.exception("Partition existence check failed.", error=str(e))
            return False

    async def list_all_chunk(self, partition: str, include_embedding: bool = True):
        """
        List all chunk from a given partition.
        """
        try:
            if not self.partition_file_manager.partition_exists(partition):
                self.logger.warning(
                    f"Partition '{partition}' does not exist or no files found."
                )
                # Raise an exception if the partition does not exist
                raise VDBPartitionNotFound(
                    f"Partition '{partition}' not found.",
                    collection_name=self.collection_name,
                    partition=partition,
                )

            # Create a filter expression for the query
            filter_expression = "partition == {partition}"
            expr_params = {"partition": partition}

            excluded_keys = ["text"]
            if not include_embedding:
                excluded_keys.append("vector")

            def prepare_metadata(res: dict):
                metadata = {}
                for k, v in res.items():
                    if k not in excluded_keys:
                        if k == "vector":
                            v = str(np.array(v).flatten().tolist())
                        metadata[k] = v
                return metadata

            chunks = []
            iterator = self._client.query_iterator(
                collection_name=self.collection_name,
                filter=filter_expression,
                expr_params=expr_params,
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

        except MilvusException as e:
            self.logger.exception("Milvus query failed", error=str(e))
            raise VDBSearchError(
                f"Milvus query failed: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except VDBError:
            raise

        except Exception as e:
            self.logger.exception(
                f"Unexpected error while listing all chunks in partition {partition}",
                error=str(e),
            )
            raise UnexpectedVDBError(
                f"Unexpected error while listing all chunks in partition {partition}: {str(e)}",
                collection_name=self.collection_name,
                partition=partition,
            )


def _parse_documents_from_search_results(search_results):
    if not search_results:
        return []

    ret = []
    excluded_keys = ["text", "vector"]
    for result in search_results[0]:
        entity = result.get("entity", {})
        metadata = {k: v for k, v in entity.items() if k not in excluded_keys}
        doc = Document(
            page_content=entity["text"],
            metadata=metadata,
        )
        ret.append(doc)

    return ret


class ConnectorFactory:
    CONNECTORS: dict[BaseVectorDB] = {
        "milvus": MilvusDB,
        # "qdrant": QdrantDB,
    }

    @staticmethod
    def get_vectordb_cls():
        name = config.vectordb.get("connector_name")
        vdb_cls = ConnectorFactory.CONNECTORS.get(name)
        if not vdb_cls:
            raise ValueError(f"VECTORDB '{name}' is not supported.")
        return vdb_cls
