import asyncio
import secrets
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import numpy as np
import ray
from config import load_config
from langchain_core.documents.base import Document
from models.user import UserCreate, UserUpdate
from pymilvus import (
    AsyncMilvusClient,
    MilvusClient,
    MilvusException,
)
from utils.exceptions.base import EmbeddingError
from utils.exceptions.vectordb import *
from utils.logger import get_logger

from ..embeddings import BaseEmbedding, EmbeddingFactory

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


@ray.remote
class MilvusDB(BaseVectorDB):
    def __init__(self):
        try:
            from config import load_config
            from services.storage.milvus_store import MilvusVectorStore
            from services.storage.postgres_store import PostgresStore
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
            self.collection_name = self.config.vectordb.collection_name
            self.logger = self.logger.bind(collection=self.collection_name, database="Milvus")

            # Collection lifecycle (create/load/version-check) is owned by
            # MilvusVectorStore — see `await self.initialize()` below, which
            # the actor bootstrap calls after ray.get_actor(...). The shim
            # no longer manages the schema directly; the store does.
            self._vector_store = MilvusVectorStore(self.config.vectordb)

            # Catalog store composite: owns the asyncpg pool + all PG repos.
            # Instantiated here (sync constructor) but its pool is opened by
            # `await self.initialize()`, which the actor bootstrap calls
            # synchronously after creating the actor handle.
            #
            # RDBConfig.database is intentionally optional — the database
            # name is derived from the Milvus collection name here, matching
            # the legacy PartitionFileManager wiring that this shim replaces.
            rdb_config = self.config.rdb.model_copy(
                update={"database": f"partitions_for_collection_{self.collection_name}"}
            )
            self._catalog_store = PostgresStore(rdb_config)

        except VDBError:
            raise

        except Exception as e:
            self.logger.exception("Unexpected error initializing Milvus clients", error=str(e))
            raise VDBConnectionError(
                f"Unexpected error initializing Milvus clients: {e!s}",
                db_url=uri,
                db_type="Milvus",
            )

    async def initialize(self) -> None:
        """Materialise the Milvus collection and open the catalog store pool.

        Called once by the actor bootstrap after `ray.get_actor(...)`.
        `MilvusVectorStore.initialize` is idempotent (double-checked locking
        on `_loaded`) and takes the embedding dimension so it can size the
        ``vector`` field on a fresh collection.
        """
        import os

        await self._vector_store.initialize(self.embedder.embedding_dimension)

        # Bootstrap: the per-collection PG database may not exist on a fresh
        # deploy. The legacy PartitionFileManager auto-created it via
        # sqlalchemy_utils; PostgresStore.initialize expects it to be there,
        # so the shim creates it here. TODO(phase-9): remove together with
        # the shim — at that point the bootstrap caller (or an operator)
        # owns DB provisioning.
        await asyncio.to_thread(self._ensure_pg_database)

        await self._catalog_store.initialize()

        # Replicate the legacy PartitionFileManager bootstrap: ensure the
        # admin user (id=1) exists with the AUTH_TOKEN hash, and cache the
        # default file quota for create_user fallback.
        # TODO(phase-9): move into a dedicated bootstrap step in the DI
        # wiring once the shim is gone.
        await self._catalog_store.user_repo.ensure_admin_user(os.getenv("AUTH_TOKEN"))
        self.file_quota_per_user = self.config.rdb.default_file_quota

    def _ensure_pg_database(self) -> None:
        """Create the per-collection PG database if it doesn't exist (sync)."""
        from sqlalchemy import URL
        from sqlalchemy_utils import create_database, database_exists

        cfg = self.config.rdb
        url = URL.create(
            drivername="postgresql",
            username=cfg.user,
            password=cfg.password,
            host=cfg.host,
            port=cfg.port,
            database=f"partitions_for_collection_{self.collection_name}",
        )
        if not database_exists(url):
            create_database(url)
            self.logger.info(f"Created PG database `partitions_for_collection_{self.collection_name}`.")

    async def list_collections(self) -> list[str]:
        # TODO(phase-9): remove. MilvusVectorStore does not expose
        # list_collections (the store services exactly one collection),
        # so the shim falls back to the raw client until the caller is
        # migrated off this multi-collection API.
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
            res = await self._catalog_store.document_repo.file_exists_in_partition(file_id=file_id, partition=partition)
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

            # record file in PG catalog (cross-cutting with Milvus insert above)
            file_metadata.update({"indexed_at": indexed_at})
            await self._catalog_store.document_repo.add_file_to_partition(
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

    @staticmethod
    def _row_to_document(row: dict[str, Any]) -> Document:
        """Adapt a MilvusVectorStore result row to a LangChain ``Document``.

        The store keeps ``text`` and drops the dense ``vector``, and adds
        ``id`` / ``score`` alongside the entity fields (including the INT64
        ``_id`` that downstream dedup keys on). Everything but ``text`` and
        ``vector`` carries through as metadata.
        """
        return Document(
            page_content=row.get("text", ""),
            metadata={k: v for k, v in row.items() if k not in ("text", "vector")},
        )

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
        # Collapse partition row-tagging, the raw `filter` escape hatch, and
        # workspace→file_id resolution into the keys MilvusVectorStore's
        # _build_filter_expr understands. The store owns expression rendering
        # and Milvus error translation now — this shim only embeds, delegates,
        # and adapts rows back to LangChain Documents.
        filters: dict[str, Any] = {}
        if partition and partition != ["all"]:
            filters["partition"] = list(partition)
        if filter:
            filters["expr"] = filter

        if filter_params:
            # Don't mutate the caller's dict — concurrent calls may share it
            filter_params = dict(filter_params)
            if "workspace_id" in filter_params:
                workspace_id = filter_params.pop(
                    "workspace_id"
                )  # workspace_id resolves through the catalog store, not Milvus directly
                ws = await self._catalog_store.workspace_repo.get_workspace_dict(workspace_id)
                if not ws:
                    return []  # Workspace not found → no results

                file_ids = await self._catalog_store.workspace_repo.list_workspace_files(workspace_id)
                if not file_ids:
                    return []  # Empty workspace → no results

                # Pin to the workspace's own partition regardless of the requested
                # partition set — file_id is only unique per (file_id, partition_name)
                # so a cross-partition search could otherwise return chunks from a
                # different partition that reuses the same file_id. Scalar value
                # overrides any partition list set above.
                filters["partition"] = ws["partition_name"]
                filters["file_id"] = file_ids

        try:
            query_vector = await self.embedder.aembed_query(query)
            # Dense vs. hybrid is a collection-build property the store owns;
            # always pass query_text so the hybrid path has its BM25 input.
            rows = await self._vector_store.search(
                query_vector,
                query_text=query,
                top_k=top_k,
                filters=filters,
                similarity_threshold=similarity_threshold,
            )

            docs = [self._row_to_document(r) for r in rows]
            if with_surrounding_chunks:
                self.logger.debug("Fetching surrounding chunks")
                surrounding_chunks = await self.get_surrounding_chunks(docs)
                self.logger.debug("Fetched surrounding chunks", count=len(surrounding_chunks))
                docs.extend(surrounding_chunks)

            return docs

        except EmbeddingError as e:
            self.logger.exception("Embedding failed while processing the query", error=str(e))
            raise
        except VDBError as e:
            self.logger.exception("Search failed in Milvus", error=str(e))
            raise e
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

        # Single store call with section_id IN [...] — the legacy code did N
        # parallel queries (one per section_id); _build_filter_expr renders
        # the list as a single `section_id in [...]` clause.
        rows = await self._vector_store.query_chunks_by_filter(
            self.collection_name,
            {"section_id": section_ids},
        )

        # Build output, skipping duplicates
        output_docs = []
        for row in rows:
            doc_id = row.get("_id")
            if doc_id is None or doc_id in existant_ids:
                continue
            existant_ids.add(doc_id)
            output_docs.append(
                Document(
                    page_content=row["text"],
                    metadata={k: v for k, v in row.items() if k not in ("text", "vector")},
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

            await self._catalog_store.workspace_repo.remove_file_from_all_workspaces(file_id, partition)
            await self._catalog_store.document_repo.remove_file_from_partition(file_id=file_id, partition=partition)
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
            await self._vector_store.delete([str(i) for i in chunk_ids])
            self.logger.info("Deleted old chunks by ID.", count=len(chunk_ids))
        except VDBError:
            self.logger.exception("Failed to delete old chunks by ID")
            raise
        except Exception as e:
            self.logger.exception("Unexpected error while deleting chunks by ID", error=str(e))
            raise UnexpectedVDBError(
                f"Unexpected error while deleting chunks by ID: {e!s}",
                collection_name=self.collection_name,
            )

    async def get_file_chunk_ids(self, file_id: str, partition: str) -> list[int]:
        """Return the Milvus _id values for all chunks of a file."""
        log = self.logger.bind(file_id=file_id, partition=partition)
        log.debug("Querying for file chunk IDs")
        try:
            ids = await self._vector_store.query_ids_by_filter(
                self.collection_name,
                {"partition": partition, "file_id": file_id},
            )
            return [int(i) for i in ids]
        except VDBError:
            log.exception("Failed to get file chunk IDs")
            raise
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
            if not await self._catalog_store.document_repo.update_file_metadata_in_db(
                file_id, partition, file_metadata
            ):
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

            # 3. Delete old chunks by _id (new ones are already durable).
            await self.delete_chunks_by_ids(old_chunk_ids)

            # 4. Update existing PostgreSQL file record in-place (preserves files.id PK)
            file_metadata.update({"indexed_at": indexed_at})
            if not await self._catalog_store.document_repo.update_file_in_partition(
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
            await self._check_file_exists(file_id, partition)

            # Milvus query with output_fields=["*"] returns all scalar fields
            # but excludes vector fields. To include vectors, request them explicitly.
            output_fields = ["*", "vector"] if include_vectors else ["*"]
            rows = await self._vector_store.query_chunks_by_filter(
                self.collection_name,
                {"partition": partition, "file_id": file_id},
                output_fields=output_fields,
            )
            # The store drains its query_iterator unbounded; preserve the
            # legacy caller-facing `limit` cap until it is pushed down into
            # query_chunks_by_filter itself.
            if len(rows) > limit:
                rows = rows[:limit]

            excluded_keys = {"text"}
            if not include_id:
                excluded_keys.add("_id")
            if not include_vectors:
                excluded_keys.add("vector")

            docs = [
                Document(
                    page_content=row["text"],
                    metadata={k: v for k, v in row.items() if k not in excluded_keys},
                )
                for row in rows
            ]
            log.info("Fetched file chunks.", count=len(rows))
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
            rows = await self._vector_store.query_chunks_by_filter(
                self.collection_name,
                {"_id": chunk_id_int},
            )
            if rows:
                row = rows[0]
                return Document(
                    page_content=row["text"],
                    metadata={key: value for key, value in row.items() if key not in ["text", "vector"]},
                )
            return None
        except VDBError:
            log.exception("Milvus query failed")
            raise
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

    async def file_exists(self, file_id: str, partition: str):
        """
        Check if a file exists in Milvus
        """
        try:
            return await self._catalog_store.document_repo.file_exists_in_partition(
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

    async def list_partition_files(self, partition: str, limit: int | None = None):
        try:
            await self._check_partition_exists(partition)
            return await self._catalog_store.document_repo.list_partition_files(partition=partition, limit=limit)

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

    async def list_partitions(self):
        try:
            return await self._catalog_store.partition_repo.list_partitions()
        except Exception as e:
            self.logger.exception("Failed to list partitions", error=str(e))
            raise

    async def collection_exists(self, collection_name: str):
        """
        Check if a collection exists in Milvus
        """
        return await self._vector_store.collection_exists(collection_name)

    async def delete_partition(self, partition: str):
        await self._check_partition_exists(partition)
        log = self.logger.bind(partition=partition)

        try:
            count = self._client.delete(
                collection_name=self.collection_name,
                filter=f"partition == '{partition}'",
            )

            await self._catalog_store.partition_repo.delete_partition(name=partition)
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

    async def partition_exists(self, partition: str):
        """
        Check if a partition exists in Milvus
        """
        log = self.logger.bind(partition=partition)
        try:
            return await self._catalog_store.partition_repo.partition_exists(name=partition)
        except Exception as e:
            log.exception("Partition existence check failed.", error=str(e))
            return False

    async def list_all_chunk(self, partition: str, include_embedding: bool = True):
        """
        List all chunk from a given partition.
        """
        try:
            await self._check_partition_exists(partition)

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

            # Drains the partition into memory in one shot. The store's
            # query_chunks_by_filter wraps Milvus's query_iterator internally,
            # so we lose the legacy batch_size=16000 streaming knob — acceptable
            # because callers already consume the result as a list.
            output_fields = ["*", "vector"] if include_embedding else ["*"]
            rows = await self._vector_store.query_chunks_by_filter(
                self.collection_name,
                {"partition": partition},
                output_fields=output_fields,
            )
            return [Document(page_content=row["text"], metadata=prepare_metadata(row)) for row in rows]

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
        fields = body.model_dump()
        file_quota = fields.get("file_quota")
        if self.file_quota_per_user > 0 and file_quota is None:
            file_quota = self.file_quota_per_user
        return await self._catalog_store.user_repo.create_legacy_user(
            display_name=fields.get("display_name"),
            external_user_id=fields.get("external_user_id"),
            email=fields.get("email"),
            is_admin=fields.get("is_admin", False),
            file_quota=file_quota,
        )

    async def get_user(self, user_id: int):
        await self._check_user_exists(user_id)
        return await self._catalog_store.user_repo.get_user_dict_by_id(user_id)

    async def delete_user(self, user_id: int):
        await self._check_user_exists(user_id)
        user_partitions = [
            p["partition"]
            for p in await self._catalog_store.membership_repo.list_user_partitions_dict(user_id)
            if p["role"] == "owner"
        ]
        for partition in user_partitions:
            await self.delete_partition(partition)
        await self._catalog_store.user_repo.delete_user(user_id)

    async def list_users(self):
        return await self._catalog_store.user_repo.list_users_dict()

    async def get_user_by_token(self, token: str):
        return await self._catalog_store.user_repo.get_user_by_token_plain(token)

    async def regenerate_user_token(self, user_id: int):
        await self._check_user_exists(user_id)
        return await self._catalog_store.user_repo.regenerate_user_token(user_id)

    async def update_user(self, user_id: int, body: UserUpdate):
        await self._check_user_exists(user_id)
        user = await self._catalog_store.user_repo.update_user(user_id, **body.model_dump(exclude_unset=True))
        if user is None:
            return None
        return {
            "id": user.id,
            "display_name": user.display_name,
            "external_user_id": user.external_user_id,
            "email": user.email,
            "is_admin": user.is_admin,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "file_quota": user.file_quota,
            "file_count": user.file_count,
        }

    async def list_user_partitions(self, user_id: int):
        await self._check_user_exists(user_id)
        return await self._catalog_store.membership_repo.list_user_partitions_dict(user_id)

    # ------------------------------------------------------------------
    # OIDC — exposed on the Ray actor (thin delegations)
    # ------------------------------------------------------------------

    async def get_user_by_external_id(self, external_user_id: str):
        return await self._catalog_store.user_repo.get_user_by_external_id_dict(external_user_id)

    async def update_user_fields(self, user_id: int, fields: dict):
        await self._catalog_store.user_repo.update_user_fields(user_id, fields)

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
        await self._check_user_exists(user_id)
        return await self._catalog_store.oidc_session_repo.create_oidc_session(
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
        return await self._catalog_store.oidc_session_repo.get_oidc_session_by_token(session_token_plain)

    async def get_oidc_session_by_id(self, session_id: int):
        return await self._catalog_store.oidc_session_repo.get_oidc_session_by_id(session_id)

    async def update_oidc_session_tokens(
        self,
        *,
        session_id: int,
        access_token_encrypted: bytes,
        refresh_token_encrypted: bytes | None,
        access_token_expires_at,
    ):
        return await self._catalog_store.oidc_session_repo.update_oidc_session_tokens(
            session_id=session_id,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            access_token_expires_at=access_token_expires_at,
        )

    async def revoke_oidc_sessions_by_sid(self, sid: str) -> int:
        return await self._catalog_store.oidc_session_repo.revoke_oidc_sessions_by_sid(sid)

    async def revoke_oidc_session_by_id(self, session_id: int) -> None:
        return await self._catalog_store.oidc_session_repo.revoke_oidc_session_by_id(session_id)

    async def cleanup_expired_oidc_sessions(self) -> int:
        return await self._catalog_store.oidc_session_repo.cleanup_expired_oidc_sessions()

    async def list_partition_members(self, partition: str) -> list[dict]:
        await self._check_partition_exists(partition)
        return await self._catalog_store.membership_repo.list_partition_members(partition)

    async def update_partition_member_role(self, partition: str, user_id: int, new_role: str):
        await self._check_membership_exists(partition, user_id)
        await self._catalog_store.membership_repo.update_partition_member_role(partition, user_id, new_role)
        self.logger.info(f"User_id {user_id} role updated to '{new_role}' in partition '{partition}'.")

    async def create_partition(self, partition: str, user_id: int):
        await self._check_user_exists(user_id)
        await self._catalog_store.partition_repo.create_partition(name=partition, user_id=user_id)
        self.logger.info(f"Partition '{partition}' created by user_id {user_id}.")

    async def add_partition_member(self, partition: str, user_id: int, role: str):
        await self._check_partition_exists(partition)
        await self._check_user_exists(user_id)
        await self._catalog_store.membership_repo.add_partition_member(partition, user_id, role)
        self.logger.info(f"User_id {user_id} added to partition '{partition}'.")

    async def remove_partition_member(self, partition: str, user_id: int) -> bool:
        await self._check_membership_exists(partition, user_id)
        await self._catalog_store.membership_repo.remove_partition_member(partition, user_id)
        self.logger.info(f"User_id {user_id} removed from partition '{partition}'.")

    async def _check_user_exists(self, user_id: int):
        if not await self._catalog_store.user_repo.user_exists(user_id):
            self.logger.warning(f"User with ID {user_id} does not exist.")
            raise VDBUserNotFound(
                f"User with ID {user_id} does not exist.",
                collection_name=self.collection_name,
                user_id=user_id,
            )

    async def _check_partition_exists(self, partition: str):
        if not await self._catalog_store.partition_repo.partition_exists(name=partition):
            self.logger.warning(f"Partition '{partition}' does not exist.")
            raise VDBPartitionNotFound(
                f"Partition '{partition}' does not exist.",
                collection_name=self.collection_name,
                partition=partition,
            )

    async def _check_membership_exists(self, partition: str, user_id: int):
        await self._check_partition_exists(partition)
        await self._check_user_exists(user_id)
        if not await self._catalog_store.membership_repo.user_is_partition_member(user_id, partition):
            raise VDBMembershipNotFound(
                f"User with ID {user_id} is not a member of partition '{partition}'.",
                collection_name=self.collection_name,
                user_id=user_id,
                partition=partition,
            )

    async def _check_file_exists(self, file_id, partition: str):
        if not await self._catalog_store.document_repo.file_exists_in_partition(file_id=file_id, partition=partition):
            raise VDBFileNotFoundError(
                f"File ID '{file_id}' does not exist in partition '{partition}'",
                collection_name=self.collection_name,
                partition=partition,
                file_id=file_id,
            )

    # Document relationship methods

    async def get_files_by_relationship(self, partition: str, relationship_id: str) -> list[dict]:
        """Get all files sharing a relationship_id within a partition.

        Args:
            partition: Partition name
            relationship_id: The relationship group identifier

        Returns:
            List of file dictionaries
        """
        return await self._catalog_store.document_repo.get_files_by_relationship(
            partition=partition, relationship_id=relationship_id
        )

    async def get_file_ancestors(
        self, partition: str, file_id: str, max_ancestor_depth: int | None = None
    ) -> list[dict]:
        """Get all ancestors of a file (direct path from root to file).

        Args:
            partition: Partition name
            file_id: The file identifier

        Returns:
            List of file dictionaries ordered from root to the specified file
        """
        return await self._catalog_store.document_repo.get_file_ancestors(
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
        file_ids = await self._catalog_store.document_repo.get_file_ids_by_relationship(
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
        ancestor_file_ids = await self._catalog_store.document_repo.get_ancestor_file_ids(
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
        await self._catalog_store.workspace_repo.create_workspace_legacy(workspace_id, partition, user_id, display_name)

    async def list_workspaces(self, partition: str) -> list[dict]:
        return await self._catalog_store.workspace_repo.list_workspaces_dict(partition)

    async def get_workspace(self, workspace_id: str) -> dict | None:
        return await self._catalog_store.workspace_repo.get_workspace_dict(workspace_id)

    async def delete_workspace(self, workspace_id: str) -> list[str]:
        """Delete workspace and return orphaned file_ids. Caller must delete those files from Milvus."""
        return await self._catalog_store.workspace_repo.delete_workspace(workspace_id)

    async def get_existing_file_ids(self, partition: str, file_ids: list[str]) -> list[str]:
        """Return the subset of file_ids that exist in the given partition."""
        return list(await self._catalog_store.workspace_repo.get_existing_file_ids(partition, file_ids))

    async def add_files_to_workspace(self, workspace_id: str, file_ids: list[str]) -> list[str]:
        return await self._catalog_store.workspace_repo.add_files_to_workspace(workspace_id, file_ids)

    async def remove_file_from_workspace(self, workspace_id: str, file_id: str) -> bool:
        return await self._catalog_store.workspace_repo.remove_file_from_workspace(workspace_id, file_id)

    async def list_workspace_files(self, workspace_id: str) -> list[str]:
        return await self._catalog_store.workspace_repo.list_workspace_files(workspace_id)

    async def get_file_workspaces(self, file_id: str, partition: str) -> list[str]:
        """Return workspace IDs that contain the given file, scoped to the partition."""
        return await self._catalog_store.workspace_repo.get_file_workspaces(file_id, partition)


def _gen_chunk_order_metadata(n: int = 20) -> list[dict]:
    # Two concurrent ingestions starting within the same nanosecond would
    # otherwise produce overlapping integer ranges (base_ts + i collides
    # across tasks), corrupting prev_section_id / next_section_id
    # navigation by stitching together chunks from different documents.
    # A cryptographically random 60-bit base makes the per-call ranges
    # disjoint with overwhelming probability while staying well under the
    # int64 ceiling Milvus uses for the section_id field. The
    # prev/next_section_id navigation only needs ordering *within* a single
    # call, so the lost wall-clock monotonicity across calls is fine.
    base = secrets.randbits(60)
    ids: list[int] = [base + i for i in range(n)]
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
