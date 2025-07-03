import asyncio
import gc
import re
import time
from pathlib import Path
from typing import Dict, Optional, Union

import ray
import torch
from config import load_config
from langchain_core.documents.base import Document
from marker.converters.pdf import PdfConverter
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from ..base import BaseLoader

logger = get_logger()
config = load_config()


@ray.remote(num_gpus=config.loader.get("marker_num_gpus", 0))
class MarkerWorker:
    def __init__(self):
        import os

        from config import load_config
        from utils.logger import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.page_sep = "[PAGE_SEP]"

        self._workers = self.config.loader.get("marker_max_processes")
        self.maxtasksperchild = self.config.loader.get("marker_max_tasks_per_child", 5)

        self.converter_config = {
            "output_format": "markdown",
            "paginate_output": True,
            "page_separator": self.page_sep,
            "pdftext_workers": 1,
            "disable_multiprocessing": True,
        }
        os.environ["RAY_ADDRESS"] = "auto"
        self.pool = None
        self.init_resources()

    def init_resources(self):
        from marker.models import create_model_dict

        self.model_dict = create_model_dict()
        for v in self.model_dict.values():
            if hasattr(v.model, "share_memory"):
                v.model.share_memory()

        self.setup_mp()

    def setup_mp(self):
        import torch.multiprocessing as mp

        if self.pool:
            self.logger.warning("Resetting multiprocessing pool")
            self.pool.close()
            self.pool.join()
            self.pool.terminate()
            self.pool = None
        try:
            if mp.get_start_method(allow_none=True) != "spawn":
                mp.set_start_method("spawn", force=True)
        except RuntimeError:
            self.logger.warning(
                "Process start method already set, using existing method"
            )

        self.logger.info(f"Initializing MarkerWorker with {self._workers} workers")
        ctx = mp.get_context("spawn")
        self.pool = ctx.Pool(
            processes=self._workers,
            initializer=self._worker_init,
            initargs=(self.model_dict,),
            maxtasksperchild=self.maxtasksperchild,
        )

        self.logger.info("MarkerWorker initialized with multiprocessing pool")

    @staticmethod
    def _worker_init(model_dict):
        global worker_model_dict
        worker_model_dict = model_dict
        logger.debug("Worker initialized with model dictionary")

    @staticmethod
    def _process_pdf(file_path, config):
        global worker_model_dict

        try:
            logger.debug("Processing PDF", path=file_path)
            converter = PdfConverter(
                artifact_dict=worker_model_dict,
                config=config,
            )
            render = converter(file_path)
            return render
        except Exception:
            logger.exception("Error processing PDF", path=file_path)
            raise
        finally:
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()

    async def process_pdf(self, file_path: str):
        from multiprocessing.context import TimeoutError as MPTimeoutError

        config = self.converter_config.copy()
        loop = asyncio.get_event_loop()

        def run_with_timeout():
            async_result = self.pool.apply_async(self._process_pdf, (file_path, config))
            try:
                result = async_result.get(
                    timeout=self.config.loader.get("marker_timeout")
                )
                return result
            except MPTimeoutError:
                self.logger.exception(
                    "MarkerWorker child process timed out", path=file_path
                )
                self.setup_mp()
                raise
            except Exception:
                self.logger.exception(
                    "Error processing with MarkerWorker", path=file_path
                )
                raise

        result = await loop.run_in_executor(None, run_with_timeout)
        return result.markdown, result.images

    def get_current_pool_size(self):
        return len([p for p in self.pool._pool if p.is_alive()])


@ray.remote
class MarkerPool:
    def __init__(self):
        from config import load_config
        from utils.logger import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.min_processes = self.config.loader.get("marker_min_processes")
        self.max_processes = self.config.loader.get("marker_max_processes")
        self.pool_size = config.loader.get("marker_pool_size")
        self.actors = [MarkerWorker.remote() for _ in range(self.pool_size)]
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(self.pool_size):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        self.logger.info(
            f"Marker pool: {self.pool_size} actors × {self.max_processes} slots = "
            f"{self.pool_size * self.max_processes} PDF concurrency"
        )

    async def ensure_worker_pool_healthy(self, worker):
        current_alive = await worker.get_current_pool_size.remote()
        if current_alive < self.min_processes:
            self.logger.warning(
                f"Only {current_alive}/{self.min_processes} worker processes alive. Reinitializing pool..."
            )
            await worker.setup_mp.remote()

    async def process_pdf(self, file_path: str):
        # Wait until any slot is free
        worker = await self._queue.get()
        if worker:
            self.logger.info("MarkerWorker allocated")
            # Ensure the worker pool is healthy
            await self.ensure_worker_pool_healthy(worker)
        try:
            markdown, images = await worker.process_pdf.remote(file_path)
            return markdown, images
        except Exception as e:
            self.logger.exception(
                "Error processing PDF with MarkerWorker", error=str(e)
            )
            raise
        finally:
            await self._queue.put(worker)
            self.logger.debug("MarkerWorker returned to pool")


class MarkerLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.page_sep = "[PAGE_SEP]"
        self.worker = ray.get_actor("MarkerPool", namespace="ragondin")

    async def aload_document(
        self,
        file_path: Union[str, Path],
        metadata: Optional[Dict] = None,
        save_markdown: bool = False,
    ) -> Document:
        if metadata is None:
            metadata = {}

        file_path_str = str(file_path)
        start = time.time()

        try:
            markdown, images = await self.worker.process_pdf.remote(file_path_str)

            if not markdown:
                raise RuntimeError(f"Conversion failed for {file_path_str}")

            if self.config["loader"]["image_captioning"]:
                captions_dict = await self._get_captions(images)
                for key, desc in captions_dict.items():
                    tag = f"![]({key})"
                    markdown = markdown.replace(tag, desc)

            else:
                logger.debug("Image captioning disabled.")

            markdown = markdown.split(self.page_sep, 1)[1]
            markdown = re.sub(
                r"\{(\d+)\}" + re.escape(self.page_sep), r"[PAGE_\1]", markdown
            )
            markdown = markdown.replace("<br>", " <br> ").strip()

            doc = Document(page_content=markdown, metadata=metadata)

            if save_markdown:
                self.save_document(doc, file_path_str)

            duration = time.time() - start
            logger.info(f"Processed {file_path_str} in {duration:.2f}s")
            return doc

        except Exception:
            logger.exception("Error in aload_document", path=file_path_str)
            raise

    async def _get_captions(self, img_dict: dict) -> dict:
        if not img_dict:
            return {}

        tasks = []
        keys = []
        for key, picture in img_dict.items():
            tasks.append(self.get_image_description(image=picture))
            keys.append(key)

        try:
            results = await tqdm.gather(*tasks, desc="Captioning images")
            assert len(keys) == len(results), "Mismatch between keys and results count"
        except asyncio.CancelledError:
            logger.warning("Image captioning tasks cancelled")
            for task in tasks:
                task.cancel()
            raise
        except Exception:
            logger.exception("Error during image captioning")
            raise
        result_dict = dict(zip(keys, results))
        return result_dict
