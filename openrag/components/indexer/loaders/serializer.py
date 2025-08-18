import asyncio
import gc
from pathlib import Path
from typing import Dict, Optional, Union

import ray
import torch
from config import load_config
from langchain_core.documents.base import Document

from . import get_loader_classes

config = load_config()

# Set ray resources
if torch.cuda.is_available():
    NUM_GPUS = config.ray.get("num_gpus")
else:  # On CPU
    NUM_GPUS = 0

POOL_SIZE = config.ray.get("pool_size")
MAX_TASKS_PER_WORKER = config.ray.get("max_tasks_per_worker")
DICT_MIMETYPES = dict(config.loader["mimetypes"])


@ray.remote(num_gpus=NUM_GPUS)
class DocSerializer:
    def __init__(self, data_dir=None, **kwargs) -> None:
        from config import load_config
        from utils.logger import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.data_dir = data_dir
        self.kwargs = kwargs
        self.kwargs["config"] = self.config
        self.save_markdown = self.config.loader.get("save_markdown", False)
        self.task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")

        # Initialize loader classes:
        self.loader_classes = get_loader_classes(config=self.config)
        self.logger.info("DocSerializer initialized.")

    async def serialize_document(
        self,
        task_id: str,
        path: Union[str, Path],
        metadata: Optional[Dict] = {},
    ) -> Document:
        # Set task state
        log = self.logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        await self.task_state_manager.set_state.remote(task_id, "SERIALIZING")

        log.info("Starting document serialization")

        p = Path(path)
        file_ext = p.suffix
        mimetype = metadata.get("mimetype", None)
        # Get appropriate loader for the file type
        if mimetype is None:
            loader_cls = self.loader_classes.get(file_ext)
        else:
            loader_cls = self.loader_classes.get(DICT_MIMETYPES.get(mimetype))

        if loader_cls is None:
            log.warning(f"No loader available for {p.name}")
            raise ValueError(f"No loader available for file type {file_ext}.")

        log.debug(f"Loading document: {p.name} with loader {loader_cls.__name__}")
        loader = loader_cls(**self.kwargs)

        try:
            # Load the doc
            doc: Document = await loader.aload_document(
                file_path=path, metadata=metadata, save_markdown=self.save_markdown
            )

            # Clean up resources
            del loader
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            log.info("Document serialized successfully")
            return doc
        except Exception:
            log.exception("Failed to serialize document")
            raise


@ray.remote
class SerializerQueue:
    def __init__(self):
        from utils.logger import get_logger

        self.logger = get_logger()
        # Spawn pool of serializer workers
        self.actors = [DocSerializer.remote() for _ in range(POOL_SIZE)]
        # Build a slot-queue: each actor appears MAX_TASKS_PER_WORKER times
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(MAX_TASKS_PER_WORKER):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        self.total_slots = POOL_SIZE * MAX_TASKS_PER_WORKER
        self.logger.info(
            f"SerializerQueue: {POOL_SIZE} actors × {MAX_TASKS_PER_WORKER} slots = "
            f"{POOL_SIZE * MAX_TASKS_PER_WORKER} all file concurrency"
        )

    async def submit_document(
        self,
        task_id: str,
        path: Union[str, Path],
        metadata: Optional[Dict] = {},
    ) -> Document:
        # wait until *any* slot is free
        log = self.logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        actor = await self._queue.get()
        if actor:
            log.info("Serializer worker allocated")
        try:
            # 2) hand off to that actor
            doc: Document = await actor.serialize_document.remote(
                task_id, path, metadata
            )
            return doc
        finally:
            # 3) always return the slot, even on error
            await self._queue.put(actor)
