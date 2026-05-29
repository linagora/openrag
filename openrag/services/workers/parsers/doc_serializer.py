"""DocSerializer Ray actor.

Moved from ``components/indexer/loaders/serializer.py``; the old module
re-exports this class for backward compatibility.
"""

from __future__ import annotations

import gc
from pathlib import Path

import ray
import torch
from langchain_core.documents.base import Document
from services.workers.parsers.legacy_loaders import get_loader_classes


@ray.remote(max_restarts=5)
class DocSerializer:
    def __init__(self, data_dir=None, **kwargs) -> None:
        from core.config import load_config
        from core.utils.logging import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.data_dir = data_dir
        self.kwargs = kwargs
        self.kwargs["config"] = self.config
        self.save_markdown = self.config.loader.save_markdown

        self.loader_classes = get_loader_classes(config=self.config)
        self.logger.info("DocSerializer initialized.")

    async def serialize_document(
        self,
        task_id: str,
        path: str | Path,
        metadata: dict | None = None,
    ) -> Document:
        metadata = metadata or {}
        log = self.logger.bind(
            file_id=metadata.get("file_id"),
            partition=metadata.get("partition"),
            task_id=task_id,
        )
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        await task_state_manager.set_state.remote(task_id, "SERIALIZING")

        log.info("Starting document serialization")

        p = Path(path)
        file_ext = p.suffix.lower()
        mimetype = metadata.get("mimetype", None)
        mimetypes = self.config.loader.mimetypes.to_dict()
        if mimetype is None:
            loader_cls = self.loader_classes.get(file_ext)
        else:
            loader_cls = self.loader_classes.get(mimetypes.get(mimetype))

        if loader_cls is None:
            log.warning(f"No loader available for {p.name}")
            raise ValueError(f"No loader available for file type {file_ext}.")

        log.debug(f"Loading document: {p.name} with loader {loader_cls.__name__}")
        loader = loader_cls(**self.kwargs)

        try:
            doc: Document = await loader.aload_document(
                file_path=path, metadata=metadata, save_markdown=self.save_markdown
            )
            del loader
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
            log.info("Document serialized successfully")
            return doc
        except Exception as e:
            log.exception("Failed to serialize document", error=str(e))
            raise


__all__ = ["DocSerializer"]
