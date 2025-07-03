import asyncio
import gc
import inspect
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import ray
import torch
from config import load_config
from langchain_core.documents.base import Document
from langchain_openai import OpenAIEmbeddings

from .chunker import BaseChunker, ChunkerFactory
from .vectordb import ConnectorFactory

save_uploaded_files = os.environ.get("SAVE_UPLOADED_FILES", "true").lower() == "true"


@ray.remote(max_restarts=-1, max_concurrency=1000, concurrency_groups={"insertion": 1})
class Indexer:
    def __init__(self):
        from utils.logger import get_logger

        self.config = load_config()
        self.logger = get_logger()

        self.embedder = OpenAIEmbeddings(
            model=self.config.embedder.get("model_name"),
            base_url=self.config.embedder.get("base_url"),
            api_key=self.config.embedder.get("api_key"),
        )

        self.serializer_queue = ray.get_actor("SerializerQueue", namespace="ragondin")

        # Initialize chunker
        self.chunker: BaseChunker = ChunkerFactory.create_chunker(
            self.config, embedder=self.embedder
        )

        self.vectordb = ConnectorFactory.create_vdb(
            self.config, self.logger, embeddings=self.embedder
        )

        self.task_state_manager = ray.get_actor(
            "TaskStateManager", namespace="ragondin"
        )

        self.default_partition = "_default"
        self.enable_insertion = self.config.vectordb["enable"]
        self.handle = ray.get_actor("Indexer", namespace="ragondin")
        self.logger.info("Indexer actor initialized.")

    async def serialize(
        self, task_id: str, path: str, metadata: Optional[Dict] = {}
    ) -> Document:
        doc: Document = await self.serializer_queue.submit_document.remote(
            task_id, path, metadata=metadata
        )
        return doc

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
    ):
        task_id = ray.get_runtime_context().get_task_id()
        file_id = metadata.get("file_id", None)
        log = self.logger.bind(file_id=file_id, partition=partition, task_id=task_id)
        log.info("Queued file for indexing.")
        try:
            await self.task_state_manager.set_state.remote(task_id, "QUEUED")

            # Set task details
            user_metadata = {
                k: v for k, v in metadata.items() if k not in {"file_id", "source"}
            }

            await self.task_state_manager.set_details.remote(
                task_id,
                file_id=metadata.get("file_id"),
                partition=partition,
                metadata=user_metadata,
            )

            # Check/normalize partition
            partition = self._check_partition_str(partition)
            metadata = {**metadata, "partition": partition}

            # Serialize
            doc = await self.serialize(task_id, path, metadata=metadata)

            # Chunk
            await self.task_state_manager.set_state.remote(task_id, "CHUNKING")
            chunks = await self.chunk(doc, str(path), task_id)

            if self.enable_insertion and chunks:
                await self.task_state_manager.set_state.remote(task_id, "INSERTING")
                await self.handle.insert_documents.remote(chunks)
                log.info(f"Document {path} indexed successfully")
            else:
                log.info(
                    f"Vectordb insertion skipped (enable_insertion={self.enable_insertion})."
                )

            # Mark task as completed
            await self.task_state_manager.set_state.remote(task_id, "COMPLETED")

        except Exception as e:
            log.exception(f"Task {task_id} failed in add_file")
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            await self.task_state_manager.set_state.remote(task_id, "FAILED")
            await self.task_state_manager.set_error.remote(task_id, tb)
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

    @ray.method(concurrency_group="insertion")
    async def insert_documents(self, chunks):
        await self.vectordb.async_add_documents(chunks)

    def delete_file(self, file_id: str, partition: str) -> bool:
        log = self.logger.bind(file_id=file_id, partition=partition)

        if not self.enable_insertion:
            log.error("Vector database is not enabled, but delete_file was called.")
            return False

        try:
            points = self.vectordb.get_file_points(file_id, partition)
            if not points:
                log.info("No points found for file_id.")
                return False

            self.vectordb.delete_file_points(points, file_id, partition)
            log.info("Deleted file from partition.")
            return True
        except Exception:
            log.exception("Error in delete_file")
            raise

    async def update_file_metadata(self, file_id: str, metadata: Dict, partition: str):
        log = self.logger.bind(file_id=file_id, partition=partition)

        if not self.enable_insertion:
            log.error(
                "Vector database is not enabled, but update_file_metadata was called."
            )
            return

        try:
            docs = self.vectordb.get_file_chunks(file_id, partition)
            for doc in docs:
                doc.metadata.update(metadata)

            self.delete_file(file_id, partition)
            await self.vectordb.async_add_documents(docs)
            log.info("Metadata updated for file.")
        except Exception:
            log.exception("Error in update_file_metadata")
            raise

    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.80,
        partition: Optional[Union[str, List[str]]] = None,
        filter: Optional[Dict] = {},
    ) -> List[Document]:
        partition_list = self._check_partition_list(partition)
        return await self.vectordb.async_search(
            query=query,
            partition=partition_list,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            filter=filter,
        )

    async def get_task_status(self, task_id: str) -> Optional[str]:
        return await self.task_state_manager.get_state.remote(task_id)

    def _check_partition_str(self, partition: Optional[str]) -> str:
        if partition is None:
            self.logger.warning("partition not provided; using default.")
            return self.default_partition
        if not isinstance(partition, str):
            raise ValueError("Partition must be a string.")
        return partition

    def delete_partition(self, partition: str):
        return self.vectordb.delete_partition(partition)

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

    async def _delegate_vdb_call(self, method_name: str, *args, **kwargs):
        method = getattr(self.vectordb, method_name, None)
        if method is None or not callable(method):
            raise AttributeError(f"Method {method_name} not found on vectordb.")
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _is_method_async(self, method_name: str) -> bool:
        method = getattr(self.vectordb, method_name, None)
        return inspect.iscoroutinefunction(method) if method else False


@dataclass
class TaskInfo:
    state: Optional[str] = None
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@ray.remote(max_restarts=-1)
class TaskStateManager:
    def __init__(self):
        self.tasks: Dict[str, TaskInfo] = {}
        self.lock = asyncio.Lock()

    async def _ensure_task(self, task_id: str) -> TaskInfo:
        """Helper to get-or-create the TaskInfo object under lock."""
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskInfo()
        return self.tasks[task_id]

    async def set_state(self, task_id: str, state: str):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.state = state

    async def set_error(self, task_id: str, tb_str: str):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.error = tb_str

    async def set_details(
        self, task_id: str, *, file_id: str, partition: int, metadata: dict
    ):
        async with self.lock:
            info = await self._ensure_task(task_id)
            info.details = {
                "file_id": file_id,
                "partition": partition,
                "metadata": metadata,
            }

    async def get_state(self, task_id: str) -> Optional[str]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.state if info else None

    async def get_error(self, task_id: str) -> Optional[str]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.error if info else None

    async def get_details(self, task_id: str) -> Optional[dict]:
        async with self.lock:
            info = self.tasks.get(task_id)
            return info.details if info else None

    async def get_all_states(self) -> Dict[str, str]:
        async with self.lock:
            return {tid: info.state for tid, info in self.tasks.items()}

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
