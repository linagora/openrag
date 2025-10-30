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

DICT_MIMETYPES = dict(config.loader["mimetypes"])


@ray.remote
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
        task_state_manager = ray.get_actor("TaskStateManager", namespace="openrag")
        await task_state_manager.set_state.remote(task_id, "SERIALIZING")

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