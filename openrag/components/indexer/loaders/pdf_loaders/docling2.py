import asyncio
import torch
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    AcceleratorDevice,
    AcceleratorOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc.document import PictureItem
from langchain_core.documents.base import Document
from tqdm.asyncio import tqdm
from utils.logger import get_logger
from config import load_config

import asyncio

from ..base import BaseLoader
import ray


logger = get_logger()
config = load_config()


if torch.cuda.is_available():
    DOCLING_NUM_GPUS = config.loader.get("docling_num_gpus", 0.01)
else:  # On CPU
    DOCLING_NUM_GPUS = 0

DOCLING_MAX_TASKS_PER_WORKER = config.loader.get("docling_max_tasks_per_worker", 2)


@ray.remote(num_gpus=DOCLING_NUM_GPUS)
class DoclingWorker:
    def __init__(self):
        img_scale = 2
        pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=True,
            generate_picture_images=True,
            images_scale=img_scale,
            # generate_table_images=True,
            # generate_page_images=True
        )
        pipeline_options.table_structure_options = TableStructureOptions(
            do_cell_matching=True, mode=TableFormerMode.ACCURATE
        )

        pipeline_options.accelerator_options = AcceleratorOptions(
            device=AcceleratorDevice.AUTO
        )
        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options, backend=PyPdfiumDocumentBackend
                )
            }
        )

    async def convert(self, file_path) -> ConversionResult:
        with torch.no_grad():
            o = await asyncio.to_thread(self.converter.convert, str(file_path))
            return o


@ray.remote
class DoclingPool:
    def __init__(self):
        from config import load_config
        from utils.logger import get_logger

        self.logger = get_logger()
        self.config = load_config()
        self.pool_size = config.loader.get("docling_pool_size", 1)

        self.actors = [DoclingWorker.remote() for _ in range(self.pool_size)]
        self._queue: asyncio.Queue[ray.actor.ActorHandle] = asyncio.Queue()

        for _ in range(DOCLING_MAX_TASKS_PER_WORKER):
            for actor in self.actors:
                self._queue.put_nowait(actor)

        total_slots = self.pool_size * DOCLING_MAX_TASKS_PER_WORKER
        self.logger.info(
            f"Docling pool: {self.pool_size} actors × {DOCLING_MAX_TASKS_PER_WORKER} slots = "
            f"{total_slots} PDF concurrency"
        )

    async def process_pdf(self, file_path: str) -> ConversionResult:
        actor: DoclingWorker = await self._queue.get()
        try:
            result = await actor.convert.remote(file_path)
            return result
        finally:
            await self._queue.put(actor)


class DoclingLoader2(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.docling_actor: DoclingPool = ray.get_actor(
            "DoclingPool", namespace="openrag"
        )

    async def aload_document(self, file_path, metadata, save_markdown=False):
        result: ConversionResult = await self.docling_actor.process_pdf.remote(
            file_path
        )
        n_pages = len(result.pages)

        s = ""
        for i in range(1, n_pages + 1):
            s += result.document.export_to_markdown(page_no=i)
            s += f"\n[PAGE_{i}]\n"

        enriched_content = s
        if self.config.loader["image_captioning"]:
            pictures = result.document.pictures
            descriptions = await self.get_captions(pictures)
            for description in descriptions:
                enriched_content = enriched_content.replace(
                    "<!-- image -->", description, 1
                )
        else:
            logger.debug("Image captioning disabled. Ignoring images.")

        doc = Document(page_content=enriched_content, metadata=metadata)
        if save_markdown:
            self.save_document(Document(page_content=enriched_content), str(file_path))
        return doc

    async def get_captions(self, pictures: list[PictureItem]):
        tasks = []
        for picture in pictures:
            tasks.append(self.get_image_description(picture.image.pil_image))
        try:
            results = await tqdm.gather(*tasks, desc="Captioning imgs")
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
        return results
