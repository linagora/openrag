import asyncio
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

import numpy as np
import ray
from config import load_config
from langchain_core.documents.base import Document
from models.user import UserCreate, UserUpdate
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
from sqlalchemy import URL
from utils.exceptions.base import EmbeddingError
from utils.exceptions.vectordb import *
from utils.logger import get_logger

from ..embeddings import BaseEmbedding, EmbeddingFactory
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
    def list_partition_files(self, partition: str, limit: int | None = None):
        pass

    @abstractmethod
    async def delete_file(self, file_id: str, partition: str):
        pass

    @abstractmethod
    async def async_add_documents(self, chunks: list[Document], user: dict):
        pass

    @abstractmethod
    async def async_search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.60,
        partition: list[str] = None,
        filter: str | None = None,
        filter_params: dict | None = None,
        with_surrounding_chunks: bool = False,
    ) -> list[Document]:
        pass

    @abstractmethod
    async def async_multi_query_search(
        self,
        partition: list[str],
        queries: list[str],
        top_k_per_query: int = 5,
        similarity_threshold: float = 0.6,
        filter: str | None = None,
        filter_params: dict | None = None,
        with_surrounding_chunks: bool = False,
    ) -> list[Document]:
        pass

    @abstractmethod
    async def list_all_chunk(self, partition: str, include_embedding: bool = True) -> list[Document]:
        pass

    @abstractmethod
    async def get_file_chunks(self, file_id: str, partition: str, include_id: bool = False, limit: int = 2000):
        pass

    @abstractmethod
    async def get_chunk_by_id(self, chunk_id: str):
        pass


SCHEMA_VERSION_PROPERTY_KEY = "openrag.schema_version"
INDEXED_TIME_FIELDS = ["created_at"]

MAX_LENGTH = 65_535

analyzer_params = {
    "tokenizer": "standard",
    "filter": [
        {
            "type": "stop",  # Specifies the filter type as stop
            "stop_words": [
                "<image_description>",
                "</image_description>",
                "[Image Placeholder]",
                "_english_",
                "_french_",
                "[CHUNK_START]",
                "[CHUNK_END]",
                "[CONTEXT]",
            ],  # Defines custom stop words and includes the English and French stop word list
        }
    ],
}


@ray.remote
class MilvusDB(BaseVectorDB):
    def __init__(self):
        try:
            from config import load_config
            from utils.logger import get_logger

            self.config = load_config()
            self.logger = get_logger()

            # init milvus clients
            self.port = self.config.vectordb.port
            self.host = self.config.vectordb.host
            uri = f"http://{self.host}:{self.port}"
            self.uri = uri
            try:
                self._client = MilvusClient(uri=uri)
                self._async_client = AsyncMilvusClient(uri=uri)
            except MilvusException as e:
                raise VDBConnectionError(
                    f"Failed to connect to Milvus: {e!s}",
                    db_url=uri,
                    db_type="Milvus",
                )

            # embedder
            self.embedder: BaseEmbedding = EmbeddingFactory.get_embedder(embeddings_config=self.config.embedder)

            self.hybrid_search = self.config.vectordb.hybrid_search
            # partition related params
            self.rdb_host = self.config.rdb.host
            self.rdb_port = self.config.rdb.port
            self.rdb_user = self.config.rdb.user
            self.rdb_password = self.config.rdb.password
            self.partition_file_manager: PartitionFileManager = None

            # Initialize collection-related attributes
            self.collection_name = self.config.vectordb.collection_name
            self.collection_loaded = False
            self.load_collection()

        except VDBError:
            raise

        except Exception as e:
            self.logger.exception("Unexpected error initializing Milvus clients", error=str(e))
            raise VDBConnectionError(
                f"Unexpected error initializing Milvus clients: {e!s}",
                db_url=uri,
                db_type="Milvus",
            )

    def load_collection(self):
        if not self.collection_loaded:
            self.logger = self.logger.bind(collection=self.collection_name, database="Milvus")
            try:
                if self._client.has_collection(self.collection_name):
                    self.logger.warning(f"Collection `{self.collection_name}` already exists. Loading it.")
                    self._check_schema_version()
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
                            f"Failed to create collection `{self.collection_name}`: {e!s}",
                            collection_name=self.collection_name,
                            operation="create_collection",
                        )
                    self._store_schema_version()
                try:
                    self._client.load_collection(self.collection_name)
                    self.collection_loaded = True
                except MilvusException as e:
                    self.logger.exception(
                        f"Failed to load collection `{self.collection_name}`",
                        error=str(e),
                    )
                    raise VDBCreateOrLoadCollectionError(
                        f"Failed to load existing collection `{self.collection_name}`: {e!s}",
                        collection_name=self.collection_name,
                        operation="load_collection",
                    )

                database_url = URL.create(
                    drivername="postgresql",
                    username=self.rdb_user,
                    password=self.rdb_password,
                    host=self.rdb_host,
                    port=self.rdb_port,
                    database=f"partitions_for_collection_{self.collection_name}",
                )
                self.partition_file_manager = PartitionFileManager(
                    database_url=database_url.render_as_string(hide_password=False),
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
                    f"Unexpected error setting collection name `{self.collection_name}`: {e!s}",
                    collection_name=self.collection_name,
                )

    def _create_schema(self):
        self.logger.info("Creating Schema")
        schema = self._client.create_schema(enable_dynamic_field=True)
        schema.add_field(field_name="_id", datatype=DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field(
            field_name="text",
            datatype=DataType.VARCHAR,
            enable_analyzer=True,
            enable_match=True,
            max_length=MAX_LENGTH,
            analyzer_params=analyzer_params,
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

        for time_field in INDEXED_TIME_FIELDS:
            schema.add_field(field_name=time_field, datatype=DataType.TIMESTAMPTZ, nullable=True)

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
        index_params.add_index(field_name="partition", index_type="INVERTED", index_name="partition_idx")

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
        # indexes for dates TIMESTAMPTZ field
        for time_field in INDEXED_TIME_FIELDS:
            index_params.add_index(
                field_name=time_field,
                index_type="STL_SORT",  # Index for TIMESTAMPTZ
                index_name=f"{time_field}_idx",
            )

        return index_params

    def _store_schema_version(self) -> None:
        """Persist the configured schema_version as a collection property after collection creation."""
        schema_version = self.config.vectordb.schema_version
        self._client.alter_collection_properties(
            collection_name=self.collection_name,
            properties={SCHEMA_VERSION_PROPERTY_KEY: str(schema_version)},
        )
        self.logger.info(f"Schema version {schema_version} stored on collection `{self.collection_name}`.")

    def _check_schema_version(self) -> None:
        """
        Read the stored schema version from collection properties and compare it
        against the configured schema_version.  Raises VDBSchemaMigrationRequiredError
        if they diverge so the application fails fast instead of silently working on a
        stale schema.
        """
        expected_version = self.config.vectordb.schema_version
        desc = self._client.describe_collection(self.collection_name)
        props = desc.get("properties", {})
        raw = props.get(SCHEMA_VERSION_PROPERTY_KEY)

        try:
            stored_version = int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            stored_version = 0

        if stored_version != expected_version:
            raise VDBSchemaMigrationRequiredError(
                f"Collection `{self.collection_name}` is at schema version {stored_version} "
                f"but the application requires version {expected_version}. "
                "Please perform the migration script.",
                collection_name=self.collection_name,
                stored_version=stored_version,
                expected_version=expected_version,
            )

        self.logger.info(f"Collection `{self.collection_name}` schema version {stored_version} — OK.")

    async def list_collections(self) -> list[str]:
        return self._client.list_collections()

    async def async_add_documents(self, chunks: list[Document], user: dict) -> None:
        """Asynchronously add documents to the vector store."""

        try:
            file_metadata = dict(chunks[0].metadata)
            file_metadata.pop("page")
            file_id, partition = (
                file_metadata.get("file_id"),
                file_metadata.get("partition"),
            )

            # Extract relationship fields (will be stored in both Milvus and PostgreSQL)
            relationship_id = file_metadata.get("relationship_id")
            parent_id = file_metadata.get("parent_id")

            self.logger.bind(
                partition=partition,
                file_id=file_id,
                filename=file_metadata.get("filename"),
            )

            # check if this file_id exists
            res = self.partition_file_manager.file_exists_in_partition(file_id=file_id, partition=partition)
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

            entities = []
            vectors = await self.embedder.aembed_documents(chunks)
            order_metadata_l: list[dict] = _gen_chunk_order_metadata(n=len(chunks))
            indexed_at = datetime.now(UTC).isoformat()

            for chunk, vector, order_metadata in zip(chunks, vectors, order_metadata_l):
                entities.append(
                    {
                        "text": chunk.page_content,
                        "vector": vector,
                        "indexed_at": indexed_at,
                        **order_metadata,
                        **chunk.metadata,
                    }
                )

            await self._async_client.insert(
                collection_name=self.collection_name,
                data=entities,
            )

            # insert file_id and partition into partition_file_manager
            file_metadata.update({"indexed_at": indexed_at})
            self.partition_file_manager.add_file_to_partition(
                file_id=file_id,
                partition=partition,
                file_metadata=file_metadata,
                user_id=user.get("id"),
                relationship_id=relationship_id,
                parent_id=parent_id,
            )
            self.logger.info(f"File '{file_id}' added to partition '{partition}'")
        except EmbeddingError as e:
            self.logger.exception("Embedding failed", error=str(e))
            raise
        except VDBError as e:
            self.logger.exception("VectorDB operation failed", error=str(e))
            raise

        except Exception as e:
            self.logger.exception("Unexpected error while adding a document", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while adding a document: {e!s}",
                collection_name=self.collection_name,
            )

    async def async_multi_query_search(
        self,
        partition,
        queries,
        top_k_per_query=5,
        similarity_threshold=0.6,
        filter=None,
        filter_params=None,
        with_surrounding_chunks=False,
    ) -> list[Document]:
        # Gather all search tasks concurrently
        search_tasks = [
            self.async_search(
                query=query,
                top_k=top_k_per_query,
                similarity_threshold=similarity_threshold,
                partition=partition,
                filter=filter,
                filter_params=filter_params,
                with_surrounding_chunks=with_surrounding_chunks,
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
        similarity_threshold: float = 0.60,
        partition: list[str] = None,
        filter: str | None = None,
        filter_params: dict | None = None,
        with_surrounding_chunks: bool = False,
    ) -> list[Document]:
        expr_parts = []
        if partition != ["all"]:
            expr_parts.append(f"partition in {partition}")

        if filter:
            expr_parts.append(filter)

        if filter_params:
            # Don't mutate the caller's dict — concurrent calls may share it
            filter_params = dict(filter_params)
            if "workspace_id" in filter_params:
                workspace_id = filter_params.pop(
                    "workspace_id"
                )  # workspace_id is only used for filtering in the partition_file_manager, not in Milvus directly
                ws = self.partition_file_manager.get_workspace(workspace_id)
                if not ws:
                    return []  # Workspace not found → no results

                file_ids = self.partition_file_manager.list_workspace_files(workspace_id)
                if not file_ids:
                    return []  # Empty workspace → no results

                # Pin to the workspace's own partition regardless of the requested
                # partition set — file_id is only unique per (file_id, partition_name)
                # so a cross-partition search could otherwise return chunks from a
                # different partition that reuses the same file_id.
                ws_partition = ws["partition_name"]
                # Replace any outer partition filter with the workspace's partition
                expr_parts = [p for p in expr_parts if not p.startswith("partition in ")]
                expr_parts.append(f'partition == "{ws_partition}"')

                id_list = ", ".join(f'"{fid}"' for fid in file_ids)
                expr_parts.append(f"file_id IN [{id_list}]")

        # Join all parts with " and " only if there are multiple conditions
        expr = " and ".join(expr_parts) if expr_parts else ""

        try:
            query_vector = await self.embedder.aembed_query(query)
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
                vector_param = {
                    "data": [query_vector],
                    "anns_field": "vector",
                    "search_params": {
                        "metric_type": "COSINE",
                        "params": {
                            "ef": 64,
                            "radius": similarity_threshold,
                            "range_filter": 1.0,
                        },
                    },
                    "limit": top_k,
                }
                response = await self._async_client.search(
                    collection_name=self.collection_name,
                    output_fields=["*"],
                    filter=expr,
                    **vector_param,
                )

            docs = _parse_documents_from_search_results(response)
            if with_surrounding_chunks:
                self.logger.debug("Fetching surrounding chunks")
                surrounding_chunks = await self.get_surrounding_chunks(docs)
                self.logger.debug("Fetched surrounding chunks", count=len(surrounding_chunks))
                docs.extend(surrounding_chunks)

            return docs

        except MilvusException as e:
            self.logger.exception("Search failed in Milvus", error=str(e))
            raise VDBSearchError(
                f"Search failed in Milvus: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except EmbeddingError as e:
            self.logger.exception("Embedding failed while processing the query", error=str(e))
            raise

        except Exception as e:
            self.logger.exception("Unexpected error occurred", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error occurred: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
            )

    async def get_surrounding_chunks(self, docs: list[Document]) -> list[Document]:
        existant_ids = {doc.metadata.get("_id") for doc in docs}

        # Collect all prev/next section IDs
        section_ids = [
            section_id
            for doc in docs
            for section_id in [
                doc.metadata.get("prev_section_id"),
                doc.metadata.get("next_section_id"),
            ]
            if section_id is not None
        ]

        if not section_ids:
            return []

        # Query all sections in parallel
        tasks = [
            self._async_client.query(
                collection_name=self.collection_name,
                filter=f"section_id == {section_id}",
                limit=1,
            )
            for section_id in section_ids
        ]
        responses = await asyncio.gather(*tasks)

        # Build output, skipping duplicates
        output_docs = []
        for response in responses:
            if not response:
                continue
            doc_id = response[0].get("_id")
            if doc_id not in existant_ids:
                existant_ids.add(doc_id)
                output_docs.append(
                    Document(
                        page_content=response[0]["text"],
                        metadata={key: value for key, value in response[0].items() if key not in ["text", "vector"]},
                    )
                )

        return output_docs

    async def delete_file(self, file_id: str, partition: str):
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            res = await self._async_client.delete(
                collection_name=self.collection_name,
                filter=f"partition == '{partition}' and file_id == '{file_id}'",
            )

            self.partition_file_manager.remove_file_from_all_workspaces(file_id, partition)
            self.partition_file_manager.remove_file_from_partition(file_id=file_id, partition=partition)
            log.info("Deleted file chunks from partition.", count=res.get("delete_count", 0))

        except MilvusException as e:
            log.exception(f"Couldn't delete file chunks for file_id {file_id}", error=str(e))
            raise VDBDeleteError(
                f"Couldn't delete file chunks for file_id {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except VDBError:
            raise
        except Exception as e:
            log.exception("Unexpected error while deleting file chunks", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting file chunks {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    async def delete_chunks_by_ids(self, chunk_ids: list[int]):
        """Delete specific Milvus chunks by their _id primary keys."""
        if not chunk_ids:
            return
        try:
            await self._async_client.delete(
                collection_name=self.collection_name,
                ids=chunk_ids,
            )
            self.logger.info("Deleted old chunks by ID.", count=len(chunk_ids))
        except MilvusException as e:
            self.logger.exception("Failed to delete old chunks by ID", error=str(e))
            raise VDBDeleteError(
                f"Failed to delete old chunks by ID: {e!s}",
                collection_name=self.collection_name,
            )
        except Exception as e:
            self.logger.exception("Unexpected error while deleting chunks by ID", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting chunks by ID: {e!s}",
                collection_name=self.collection_name,
            )

    async def get_file_chunk_ids(self, file_id: str, partition: str) -> list[int]:
        """Return the Milvus _id values for all chunks of a file."""
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            results = []
            offset = 0
            limit = 100
            while True:
                response = await self._async_client.query(
                    collection_name=self.collection_name,
                    filter="partition == {partition} and file_id == {file_id}",
                    filter_params={"partition": partition, "file_id": file_id},
                    output_fields=["_id"],
                    limit=limit,
                    offset=offset,
                )
                if not response:
                    break
                results.extend(r["_id"] for r in response)
                offset += len(response)
            return results
        except MilvusException as e:
            log.exception("Failed to get file chunk IDs", error=str(e))
            raise VDBSearchError(
                f"Failed to get file chunk IDs for {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except Exception as e:
            log.exception("Unexpected error while getting file chunk IDs", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while getting file chunk IDs for {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    async def upsert_file_metadata(self, file_id: str, partition: str, metadata: dict):
        """Update metadata on all chunks of a file in-place via Milvus upsert.

        Fetches existing chunks (with _id and vectors), merges new metadata,
        then upserts back into Milvus. No re-embedding is performed.
        Also updates the PostgreSQL file record metadata in-place.
        """
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            # Fetch all chunks with their _id and vector so we can upsert without re-embedding.
            docs = await self.get_file_chunks(file_id, partition, include_id=True, include_vectors=True)
            if not docs:
                log.warning("No chunks found for metadata upsert")
                return

            entities = []
            for doc in docs:
                chunk_metadata = dict(doc.metadata)
                # Merge new metadata into the chunk metadata.
                # _id and vector are already in chunk_metadata (via include_id/include_vectors).
                chunk_metadata.update(metadata)
                entities.append(
                    {
                        "text": doc.page_content,
                        **chunk_metadata,
                    }
                )

            await self._async_client.upsert(
                collection_name=self.collection_name,
                data=entities,
            )

            # Build file-level metadata from the first chunk (same as async_add_documents).
            # Strip per-chunk fields that don't belong in the file-level PG record.
            file_metadata = dict(docs[0].metadata)
            for key in ("_id", "vector", "page", "section_id", "prev_section_id", "next_section_id"):
                file_metadata.pop(key, None)
            file_metadata.update(metadata)
            if not self.partition_file_manager.update_file_metadata_in_db(file_id, partition, file_metadata):
                # PG row was concurrently deleted; Milvus upsert already succeeded.
                # Log warning but don't fail — Milvus data will be orphaned until
                # next cleanup, but the user-facing operation should still succeed.
                log.warning("PG file row not found during metadata upsert; Milvus updated but PG skipped")

            log.info("Upserted file metadata in-place.", chunk_count=len(entities))

        except MilvusException as e:
            log.exception("Milvus upsert failed", error=str(e))
            raise VDBInsertError(
                f"Couldn't upsert metadata for file {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except VDBError:
            raise
        except Exception as e:
            log.exception("Unexpected error during metadata upsert", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error during metadata upsert for {file_id}: {e!s}",
                collection_name=self.collection_name,
            )

    async def add_documents_for_existing_file(self, chunks: list[Document], user: dict) -> None:
        """Replace Milvus chunks for a file that already exists in PostgreSQL.

        Used by PUT (file replace). The flow is insert-before-delete so the file
        is never left in a half-replaced state:
        1. Snapshot old chunk _id values
        2. Embed and insert new chunks
        3. Delete old chunks by _id
        4. Update the PostgreSQL File row in-place

        If step 2 fails, old chunks remain intact. If step 3 fails, we have
        duplicates temporarily but no data loss — a retry or manual cleanup
        can resolve it.

        Note: this implements strict PUT semantics — the new chunk metadata
        fully replaces the old. Fields like ``relationship_id`` and ``parent_id``
        are taken from the new chunks' metadata; if the caller omits them, the
        PG columns are cleared. To preserve old values across a PUT, the caller
        must re-supply them in the request metadata.
        """
        log = self.logger  # Fallback; rebound with context below
        try:
            file_metadata = dict(chunks[0].metadata)
            file_metadata.pop("page")
            file_id, partition = file_metadata.get("file_id"), file_metadata.get("partition")
            relationship_id = file_metadata.get("relationship_id")
            parent_id = file_metadata.get("parent_id")

            log = self.logger.bind(partition=partition, file_id=file_id, filename=file_metadata.get("filename"))

            # 1. Snapshot old chunk _id values before inserting new ones.
            old_chunk_ids = await self.get_file_chunk_ids(file_id, partition)

            # 2. Embed and insert new chunks.
            entities = []
            vectors = await self.embedder.aembed_documents(chunks)
            order_metadata_l: list[dict] = _gen_chunk_order_metadata(n=len(chunks))
            for chunk, vector, order_metadata in zip(chunks, vectors, order_metadata_l):
                entities.append(
                    {
                        "text": chunk.page_content,
                        "vector": vector,
                        **order_metadata,
                        **chunk.metadata,
                    }
                )

            await self._async_client.insert(
                collection_name=self.collection_name,
                data=entities,
            )

            # 3. Delete old chunks by _id (new ones are already durable).
            await self.delete_chunks_by_ids(old_chunk_ids)

            # 4. Update existing PostgreSQL file record in-place (preserves files.id PK)
            if not self.partition_file_manager.update_file_in_partition(
                file_id=file_id,
                partition=partition,
                file_metadata=file_metadata,
                relationship_id=relationship_id,
                parent_id=parent_id,
            ):
                # PG row was concurrently deleted after we inserted new Milvus chunks.
                # Log warning — Milvus has new orphaned chunks but data is consistent
                # (old chunks deleted, new chunks inserted, no PG record).
                log.warning("PG file row not found during replace; Milvus updated but PG skipped")
            log.info(f"File '{file_id}' chunks replaced in partition '{partition}'")

        except EmbeddingError as e:
            log.exception("Embedding failed", error=str(e))
            raise
        except VDBError as e:
            log.exception("VectorDB operation failed", error=str(e))
            raise
        except Exception as e:
            log.exception("Unexpected error while adding chunks for existing file", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while adding chunks for existing file: {e!s}",
                collection_name=self.collection_name,
            )

    async def get_file_chunks(
        self,
        file_id: str,
        partition: str,
        include_id: bool = False,
        include_vectors: bool = False,
        limit: int = 2000,
    ):
        log = self.logger.bind(file_id=file_id, partition=partition)
        try:
            self._check_file_exists(file_id, partition)
            filter_expr = f'partition == "{partition}" and file_id == "{file_id}"'
            excluded_keys = {"text"}
            if not include_id:
                excluded_keys.add("_id")
            if not include_vectors:
                excluded_keys.add("vector")

            # Milvus query with output_fields=["*"] returns all scalar fields
            # but excludes vector fields. To include vectors, request them explicitly.
            output_fields = ["*", "vector"] if include_vectors else ["*"]

            results = []
            iterator = self._client.query_iterator(
                collection_name=self.collection_name,
                filter=filter_expr,
                limit=limit,
                batch_size=min(limit, 16000),
                output_fields=output_fields,
            )
            try:
                while True:
                    batch = iterator.next()
                    if not batch:
                        break
                    results.extend(batch)
            finally:
                iterator.close()

            docs = [
                Document(
                    page_content=res["text"],
                    metadata={key: value for key, value in res.items() if key not in excluded_keys},
                )
                for res in results
            ]
            log.info("Fetched file chunks.", count=len(results))
            return docs

        except MilvusException as e:
            log.exception(f"Couldn't get file chunks for file_id {file_id}", error=str(e))
            raise VDBSearchError(
                f"Couldn't get file chunks for file_id {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )
        except VDBError:
            raise

        except Exception as e:
            log.exception("Unexpected error while getting file chunks", error=str(e))
            raise VDBSearchError(
                f"Unexpected error while getting file chunks {file_id}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    async def get_chunk_by_id(self, chunk_id: str):
        """
        Retrieve a chunk by its ID.
        Args:
            chunk_id (str): The ID of the chunk to retrieve (Milvus Int64 _id as string).
        Returns:
            Document: The retrieved chunk, or None if not found or invalid ID format.
        """
        log = self.logger.bind(chunk_id=chunk_id)
        # Milvus _id is Int64, so we need to convert the string to int
        try:
            chunk_id_int = int(chunk_id)
        except (ValueError, TypeError):
            log.warning("Invalid chunk_id format - must be an integer")
            return None

        try:
            response = await self._async_client.query(
                collection_name=self.collection_name,
                filter=f"_id == {chunk_id_int}",
                limit=1,
            )
            if response:
                return Document(
                    page_content=response[0]["text"],
                    metadata={key: value for key, value in response[0].items() if key not in ["text", "vector"]},
                )
            return None
        except MilvusException as e:
            log.exception("Milvus query failed", error=str(e))
            raise VDBSearchError(
                f"Milvus query failed: {e!s}",
                collection_name=self.collection_name,
            )

        except Exception as e:
            log.exception("Unexpected error while retrieving chunk", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while retrieving chunk {chunk_id}: {e!s}",
                collection_name=self.collection_name,
            )

    def file_exists(self, file_id: str, partition: str):
        """
        Check if a file exists in Milvus
        """
        try:
            return self.partition_file_manager.file_exists_in_partition(file_id=file_id, partition=partition)
        except Exception as e:
            self.logger.exception(
                "File existence check failed.",
                file_id=file_id,
                partition=partition,
                error=str(e),
            )
            return False

    def list_partition_files(self, partition: str, limit: int | None = None):
        try:
            self._check_partition_exists(partition)
            return self.partition_file_manager.list_partition_files(partition=partition, limit=limit)

        except VDBError:
            raise

        except Exception as e:
            self.logger.exception(
                f"Unexpected error while listing files in partition {partition}",
                error=str(e),
            )
            raise UnexpectedVDBError(
                f"Unexpected error while listing files in partition {partition}: {e!s}",
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
        self._check_partition_exists(partition)
        log = self.logger.bind(partition=partition)

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
                f"Failed to delete partition `{partition}`: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
            )
        except VDBError as e:
            log.exception("VectorDB operation failed", error=str(e))
            raise e
        except Exception as e:
            log.exception("Unexpected error while deleting partition", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting partition {partition}: {e!s}",
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
            self._check_partition_exists(partition)

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

            try:
                while True:
                    result = iterator.next()
                    if not result:
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
            finally:
                iterator.close()

            return chunks

        except MilvusException as e:
            self.logger.exception("Milvus query failed", error=str(e))
            raise VDBSearchError(
                f"Milvus query failed: {e!s}",
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
                f"Unexpected error while listing all chunks in partition {partition}: {e!s}",
                collection_name=self.collection_name,
                partition=partition,
            )

    async def create_user(self, body: UserCreate):
        return self.partition_file_manager.create_user(body)

    async def get_user(self, user_id: int):
        self._check_user_exists(user_id)
        return self.partition_file_manager.get_user_by_id(user_id)

    async def delete_user(self, user_id: int):
        self._check_user_exists(user_id)
        user_partitions = [
            p["partition"] for p in self.partition_file_manager.list_user_partitions(user_id) if p["role"] == "owner"
        ]
        for partition in user_partitions:
            await self.delete_partition(partition)
        self.partition_file_manager.delete_user(user_id)

    async def list_users(self):
        return self.partition_file_manager.list_users()

    async def get_user_by_token(self, token: str):
        return self.partition_file_manager.get_user_by_token(token)

    async def regenerate_user_token(self, user_id: int):
        self._check_user_exists(user_id)
        return self.partition_file_manager.regenerate_user_token(user_id)

    async def update_user(self, user_id: int, body: UserUpdate):
        self._check_user_exists(user_id)
        return self.partition_file_manager.update_user(user_id, body)

    async def list_user_partitions(self, user_id: int):
        self._check_user_exists(user_id)
        return self.partition_file_manager.list_user_partitions(user_id)

    # ------------------------------------------------------------------
    # OIDC — exposed on the Ray actor (thin delegations)
    # ------------------------------------------------------------------

    async def get_user_by_external_id(self, external_user_id: str):
        return self.partition_file_manager.get_user_by_external_id(external_user_id)

    async def update_user_fields(self, user_id: int, fields: dict):
        self._check_user_exists(user_id)
        return self.partition_file_manager.update_user_fields(user_id, fields)

    async def create_oidc_session(
        self,
        *,
        user_id: int,
        sub: str,
        sid: str | None,
        session_token_plain: str,
        id_token_encrypted: bytes | None,
        access_token_encrypted: bytes | None,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at,
        session_expires_at,
    ):
        self._check_user_exists(user_id)
        return self.partition_file_manager.create_oidc_session(
            user_id=user_id,
            sub=sub,
            sid=sid,
            session_token_plain=session_token_plain,
            id_token_encrypted=id_token_encrypted,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            access_token_expires_at=access_token_expires_at,
            session_expires_at=session_expires_at,
        )

    async def get_oidc_session_by_token(self, session_token_plain: str):
        return self.partition_file_manager.get_oidc_session_by_token(session_token_plain)

    async def get_oidc_session_by_id(self, session_id: int):
        return self.partition_file_manager.get_oidc_session_by_id(session_id)

    async def update_oidc_session_tokens(
        self,
        *,
        session_id: int,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at,
    ):
        return self.partition_file_manager.update_oidc_session_tokens(
            session_id=session_id,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            access_token_expires_at=access_token_expires_at,
        )

    async def revoke_oidc_sessions_by_sid(self, sid: str) -> int:
        return self.partition_file_manager.revoke_oidc_sessions_by_sid(sid)

    async def revoke_oidc_session_by_id(self, session_id: int) -> None:
        return self.partition_file_manager.revoke_oidc_session_by_id(session_id)

    async def cleanup_expired_oidc_sessions(self) -> int:
        return self.partition_file_manager.cleanup_expired_oidc_sessions()

    async def list_partition_members(self, partition: str) -> list[dict]:
        self._check_partition_exists(partition)
        return self.partition_file_manager.list_partition_members(partition)

    async def update_partition_member_role(self, partition: str, user_id: int, new_role: str):
        self._check_membership_exists(partition, user_id)
        self.partition_file_manager.update_partition_member_role(partition, user_id, new_role)
        self.logger.info(f"User_id {user_id} role updated to '{new_role}' in partition '{partition}'.")

    async def create_partition(self, partition: str, user_id: int):
        self._check_user_exists(user_id)
        self.partition_file_manager.create_partition(partition, user_id)
        self.logger.info(f"Partition '{partition}' created by user_id {user_id}.")

    async def add_partition_member(self, partition: str, user_id: int, role: str):
        self._check_partition_exists(partition)
        self._check_user_exists(user_id)
        self.partition_file_manager.add_partition_member(partition, user_id, role)
        self.logger.info(f"User_id {user_id} added to partition '{partition}'.")

    async def remove_partition_member(self, partition: str, user_id: int) -> bool:
        self._check_membership_exists(partition, user_id)
        self.partition_file_manager.remove_partition_member(partition, user_id)
        self.logger.info(f"User_id {user_id} removed from partition '{partition}'.")

    def _check_user_exists(self, user_id: int):
        if not self.partition_file_manager.user_exists(user_id):
            self.logger.warning(f"User with ID {user_id} does not exist.")
            raise VDBUserNotFound(
                f"User with ID {user_id} does not exist.",
                collection_name=self.collection_name,
                user_id=user_id,
            )

    def _check_partition_exists(self, partition: str):
        if not self.partition_file_manager.partition_exists(partition):
            self.logger.warning(f"Partition '{partition}' does not exist.")
            raise VDBPartitionNotFound(
                f"Partition '{partition}' does not exist.",
                collection_name=self.collection_name,
                partition=partition,
            )

    def _check_membership_exists(self, partition: str, user_id: int):
        self._check_partition_exists(partition)
        self._check_user_exists(user_id)
        if not self.partition_file_manager.user_is_partition_member(user_id, partition):
            raise VDBMembershipNotFound(
                f"User with ID {user_id} is not a member of partition '{partition}'.",
                collection_name=self.collection_name,
                user_id=user_id,
                partition=partition,
            )

    def _check_file_exists(self, file_id, partition: str):
        if not self.partition_file_manager.file_exists_in_partition(file_id=file_id, partition=partition):
            raise VDBFileNotFoundError(
                f"File ID '{file_id}' does not exist in partition '{partition}'",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    # Document relationship methods

    def get_files_by_relationship(self, partition: str, relationship_id: str) -> list[dict]:
        """Get all files sharing a relationship_id within a partition.

        Args:
            partition: Partition name
            relationship_id: The relationship group identifier

        Returns:
            List of file dictionaries
        """
        return self.partition_file_manager.get_files_by_relationship(
            partition=partition, relationship_id=relationship_id
        )

    def get_file_ancestors(self, partition: str, file_id: str, max_ancestor_depth: int | None = None) -> list[dict]:
        """Get all ancestors of a file (direct path from root to file).

        Args:
            partition: Partition name
            file_id: The file identifier

        Returns:
            List of file dictionaries ordered from root to the specified file
        """
        return self.partition_file_manager.get_file_ancestors(
            partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
        )

    async def get_related_chunks(self, partition: str, relationship_id: str, limit: int = 100) -> list[Document]:
        """Get all chunks for files in a relationship group.

        Args:
            partition: Partition name
            relationship_id: The relationship group identifier
            limit: Maximum number of chunks to return

        Returns:
            List of Document objects
        """
        file_ids = self.partition_file_manager.get_file_ids_by_relationship(
            partition=partition, relationship_id=relationship_id
        )

        if not file_ids:
            return []

        # Build filter expression for Milvus query
        file_id_list = ", ".join(f'"{fid}"' for fid in file_ids)
        filter_expr = f'partition == "{partition}" and file_id in [{file_id_list}]'

        results = await self._async_client.query(
            collection_name=self.collection_name,
            filter=filter_expr,
            limit=limit,
            output_fields=["*"],
        )

        return [
            Document(
                page_content=res["text"],
                metadata={k: v for k, v in res.items() if k not in ["text", "vector"]},
            )
            for res in results
        ]

    async def get_ancestor_chunks(
        self, partition: str, file_id: str, limit: int = 100, max_ancestor_depth: int | None = None
    ) -> list[Document]:
        """Get all chunks for ancestor files (direct path from root to file).

        Args:
            partition: Partition name
            file_id: The file identifier
            limit: Maximum number of chunks to return

        Returns:
            List of Document objects ordered by ancestry
        """
        ancestor_file_ids = self.partition_file_manager.get_ancestor_file_ids(
            partition=partition, file_id=file_id, max_ancestor_depth=max_ancestor_depth
        )

        if not ancestor_file_ids:
            return []

        # Build filter expression for Milvus query
        file_id_list = ", ".join(f'"{fid}"' for fid in ancestor_file_ids)
        filter_expr = f'partition == "{partition}" and file_id in [{file_id_list}]'

        results = await self._async_client.query(
            collection_name=self.collection_name,
            filter=filter_expr,
            limit=limit,
            output_fields=["*"],
        )

        return [
            Document(
                page_content=res["text"],
                metadata={k: v for k, v in res.items() if k not in ["text", "vector"]},
            )
            for res in results
        ]

    # --- Workspace methods ---

    async def create_workspace(
        self, workspace_id: str, partition: str, user_id: int | None = None, display_name: str | None = None
    ):
        self.partition_file_manager.create_workspace(workspace_id, partition, user_id, display_name)

    async def list_workspaces(self, partition: str) -> list[dict]:
        return self.partition_file_manager.list_workspaces(partition)

    async def get_workspace(self, workspace_id: str) -> dict | None:
        return self.partition_file_manager.get_workspace(workspace_id)

    async def delete_workspace(self, workspace_id: str) -> list[str]:
        """Delete workspace and return orphaned file_ids. Caller must delete those files from Milvus."""
        return self.partition_file_manager.delete_workspace(workspace_id)

    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> list[str]:
        """Return the subset of file_ids that exist in the given partition."""
        return list(self.partition_file_manager.get_existing_file_ids(partition, file_ids))

    async def add_files_to_workspace(self, workspace_id: str, file_ids: list[str]) -> list[str]:
        return self.partition_file_manager.add_files_to_workspace(workspace_id, file_ids)

    async def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool:
        return self.partition_file_manager.remove_file_from_workspace(workspace_id, file_id)

    async def list_workspace_files(self, workspace_id: str) -> list[str]:
        return self.partition_file_manager.list_workspace_files(workspace_id)

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        """Return workspace IDs that contain the given file, scoped to the partition."""
        return self.partition_file_manager.get_file_workspaces(file_id, partition)


def _gen_chunk_order_metadata(n: int = 20) -> list[dict]:
    # Use base timestamp + index to ensure uniqueness
    base_ts = int(time.time_ns())
    ids: list[int] = [base_ts + i for i in range(n)]
    L = []
    for i in range(n):
        prev_chunk_id = ids[i - 1] if i > 0 else None
        next_chunk_id = ids[i + 1] if i < n - 1 else None
        L.append(
            {
                "prev_section_id": prev_chunk_id,
                "section_id": ids[i],
                "next_section_id": next_chunk_id,
            }
        )
    return L


def _parse_documents_from_search_results(search_results) -> list[Document]:
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
        name = config.vectordb.connector_name
        vdb_cls = ConnectorFactory.CONNECTORS.get(name)
        if not vdb_cls:
            raise ValueError(f"VECTORDB '{name}' is not supported.")
        return vdb_cls
