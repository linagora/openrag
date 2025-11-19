import asyncio
import gc
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import ray
import torch
from config import load_config
from langchain_core.documents.base import Document

from .chunker import BaseChunker, ChunkerFactory
from components.files import serialize_file


config = load_config()
save_uploaded_files = os.environ.get("SAVE_UPLOADED_FILES", "true").lower() == "true"

POOL_SIZE = config.ray.get("pool_size")
MAX_TASKS_PER_WORKER = config.ray.get("max_tasks_per_worker")


@ray.remote(
    max_concurrency=config.ray.indexer.concurrency_groups.default,
    max_task_retries=config.ray.indexer.max_task_retries,
    concurrency_groups={
        "update": config.ray.indexer.concurrency_groups["update"],
        "search": config.ray.indexer.concurrency_groups["search"],
        "delete": config.ray.indexer.concurrency_groups["delete"],
        "insert": config.ray.indexer.concurrency_groups["insert"],
        "chunk": config.ray.indexer.concurrency_groups["chunk"],
    },
)
class Indexer:
    def __init__(self):
        from utils.logger import get_logger

        self.config = load_config()
        self.logger = get_logger()

        # Initialize chunker
        self.chunker: BaseChunker = ChunkerFactory.create_chunker(self.config)

        self.default_partition = "_default"
        self.enable_insertion = self.config.vectordb["enable"]
        self.handle = ray.get_actor("Indexer", namespace="openrag")

        self.logger.info("Indexer actor initialized.")

    @ray.method(concurrency_group="chunk")
    async def chunk(
        self, doc: Document, file_path: str, task_id: str = None
    ) -> List[Document]:
        chunks = await self.chunker.split_document(doc, task_id)
        return chunks

    async def add_file(
        self,
        path: Union[str, List[str]],
        metadata: Optional[Dict] = {},
        partition: Optional[str] = None,
        user: Optional[Dict] = None,
    ):
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        task_id = ray.get_runtime_context().get_task_id()
        file_id = metadata.get("file_id", None)
        log = self.logger.bind(file_id=file_id, partition=partition, task_id=task_id)
        log.info("Queued file for indexing.")
        try:
            # Set task details
            user_metadata = {
                k: v for k, v in metadata.items() if k not in {"file_id", "source"}
            }

            await task_state_manager.set_details.remote(
                task_id,
                file_id=metadata.get("file_id"),
                partition=partition,
                metadata=user_metadata,
                user_id=user.get("id"),
            )

            # Check/normalize partition
            partition = self._check_partition_str(partition)
            metadata = {**metadata, "partition": partition}

            # Serialize
            doc = await serialize_file(task_id, path, metadata=metadata)

            # Chunk
            await task_state_manager.set_state.remote(task_id, "CHUNKING")
            chunks = await self.handle.chunk.remote(doc, str(path), task_id)

            if self.enable_insertion:
                if chunks:
                    await task_state_manager.set_state.remote(task_id, "INSERTING")
                    await self.handle.insert_documents.remote(chunks, user=user)
                    log.info(f"Document {path} indexed successfully")
                else:
                    log.debug(
                        "No chunks to insert !!! Potentially the uploaded file is empty"
                    )
            else:
                log.info(
                    f"Vectordb insertion skipped (enable_insertion={self.enable_insertion})."
                )

            # Mark task as completed
            await task_state_manager.set_state.remote(task_id, "COMPLETED")

        except Exception as e:
            log.exception(f"Task {task_id} failed in add_file")
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            await task_state_manager.set_state.remote(task_id, "FAILED")
            await task_state_manager.set_error.remote(task_id, tb)
            raise

        finally:
            if torch.cuda.is_available():
                gc.collect()
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            try:
                # Cleanup input file
                if not save_uploaded_files:
                    Path(path).unlink(missing_ok=True)
                    log.debug(f"Deleted input file: {path}")
            except Exception as cleanup_err:
                log.warning(f"Failed to delete input file {path}: {cleanup_err}")
        return True

    @ray.method(concurrency_group="insert")
    async def insert_documents(self, chunks, user):
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        await vectordb.async_add_documents.remote(chunks, user)

    @ray.method(concurrency_group="delete")
    async def delete_file(self, file_id: str, partition: str) -> bool:
        log = self.logger.bind(file_id=file_id, partition=partition)
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        if not self.enable_insertion:
            log.error("Vector database is not enabled, but delete_file was called.")
            return False

        try:
            await vectordb.delete_file.remote(file_id, partition)
            log.info(
                "Deleted file from partition.", file_id=file_id, partition=partition
            )

        except Exception as e:
            log.exception("Error in delete_file", error=str(e))
            raise

    @ray.method(concurrency_group="update")
    async def update_file_metadata(
        self,
        file_id: str,
        metadata: Dict,
        partition: str,
        user: Optional[Dict] = None,
    ):
        log = self.logger.bind(file_id=file_id, partition=partition)
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        if not self.enable_insertion:
            log.error(
                "Vector database is not enabled, but update_file_metadata was called."
            )
            return

        try:
            docs = await vectordb.get_file_chunks.remote(file_id, partition)
            for doc in docs:
                doc.metadata.update(metadata)

            await self.delete_file(file_id, partition)
            await vectordb.async_add_documents.remote(docs, user=user)

            log.info("Metadata updated for file.")
        except Exception as e:
            log.exception("Error in update_file_metadata", error=str(e))
            raise

    @ray.method(concurrency_group="update")
    async def copy_file(
        self,
        file_id: str,
        metadata: Dict,
        partition: str,
        user: Optional[Dict] = None,
    ):
        log = self.logger.bind(file_id=file_id, partition=partition)
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        if not self.enable_insertion:
            log.error(
                "Vector database is not enabled, but update_file_metadata was called."
            )
            return

        try:
            docs = await vectordb.get_file_chunks.remote(file_id, partition)
            for doc in docs:
                doc.metadata.update(metadata)

            await vectordb.async_add_documents.remote(docs, user=user)

            log.info(
                "File copy completed",
                file_id=file_id,
                partition=partition,
                new_file_id=metadata.get("file_id"),
                new_partition=metadata.get("partition"),
            )
        except Exception as e:
            log.exception("Error in copy_file", error=str(e))
            raise

    @ray.method(concurrency_group="search")
    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.80,
        partition: Optional[Union[str, List[str]]] = None,
        filter: Optional[Dict] = {},
    ) -> List[Document]:
        partition_list = self._check_partition_list(partition)
        vectordb = ray.get_actor("Vectordb", namespace="openrag")
        return await vectordb.async_search.remote(
            query=query,
            partition=partition_list,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            filter=filter,
        )

    def _check_partition_str(self, partition: Optional[str]) -> str:
        if partition is None:
            self.logger.warning("partition not provided; using default.")
            return self.default_partition
        if not isinstance(partition, str):
            raise ValueError("Partition must be a string.")
        return partition

    def _check_partition_list(
        self, partition: Optional[Union[str, List[str]]]
    ) -> List[str]:
        if partition is None:
            self.logger.warning("partition not provided; using default.")
            return [self.default_partition]
        if isinstance(partition, str):
            return [partition]
        if isinstance(partition, list) and all(isinstance(p, str) for p in partition):
            return partition
        raise ValueError("Partition must be a string or a list of strings.")


@dataclass
class TaskInfo:
    state: Optional[str] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    object_ref: Optional[ray.ObjectRef] = None


@ray.remote(concurrency_groups={"set": 1000, "get": 1000, "queue_info": 1000})
class TaskStateManager:
    def __init__(self):
        self.tasks: Dict[str, TaskInfo] = {}
        self.user_index: Dict[int, set[str]] = {}
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        """Helper to get-or-create the TaskInfo object under lock."""
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    @ray.method(concurrency_group="set")
    async def set_state(self, task_id: str, state: str):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.state = state

    @ray.method(concurrency_group="set")
    async def set_error(self, task_id: str, tb_str: str):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.error = tb_str

    @ray.method(concurrency_group="set")
    async def set_details(
        self,
        task_id: str,
        *,
        file_id: str,
        partition: int,
        metadata: dict,
        user_id: int,
    ):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.details = {
                "file_id": file_id,
                "partition": partition,
                "metadata": metadata,
                "user_id": user_id,
            }
            self.user_index.setdefault(user_id, set()).add(task_id)

    @ray.method(concurrency_group="set")
    async def set_object_ref(self, task_id: str, object_ref: dict):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.object_ref = object_ref

    @ray.method(concurrency_group="get")
    async def get_state(self, task_id: str) -> Optional[str]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    @ray.method(concurrency_group="get")
    async def get_error(self, task_id: str) -> Optional[str]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    @ray.method(concurrency_group="get")
    async def get_details(self, task_id: str) -> Optional[dict]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    @ray.method(concurrency_group="get")
    async def get_object_ref(self, task_id: str) -> Optional[dict]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.object_ref if info else None

    @ray.method(concurrency_group="queue_info")
    async def get_all_states(self) -> Dict[str, str]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

    @ray.method(concurrency_group="queue_info")
    async def get_all_info(self) -> Dict[str, dict]:
        async with self.lock:
            return {
                task_id: {
                    "state": info.state,
                    "error": info.error,
                    "details": info.details,
                }
                for task_id, info in self.tasks.items()
            }

    @ray.method(concurrency_group="queue_info")
    async def get_all_user_info(self, user_id: int) -> Dict[str, dict]:
        async with self.lock:
            task_ids = self.user_index.get(user_id, set())
            return {
                tid: {
                    "state": self.tasks[tid].state,
                    "error": self.tasks[tid].error,
                    "details": self.tasks[tid].details,
                }
                for tid in task_ids
                if tid in self.tasks
            }

    @ray.method(concurrency_group="queue_info")
    async def get_pool_info(self) -> Dict[str, int]:
        return {
            "pool_size": POOL_SIZE,
            "max_tasks_per_worker": MAX_TASKS_PER_WORKER,
            "total_capacity": POOL_SIZE * MAX_TASKS_PER_WORKER,
        }
