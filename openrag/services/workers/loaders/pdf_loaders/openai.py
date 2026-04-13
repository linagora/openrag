import asyncio
import base64
import io
import json
import time
from abc import ABC, abstractmethod
from pathlib import Path

import pypdfium2 as pdfium
from langchain.schema import Document
from langchain_openai import ChatOpenAI
from PIL import Image
from utils.logger import logger

from ..base import BaseLoader


async def pdf_to_images(pdf_path: str, scale: float = 1.0) -> list[Image.Image]:
    pdf: pdfium.PdfDocument = await asyncio.to_thread(pdfium.PdfDocument, pdf_path)
    return [p.render(scale=scale).to_pil() for p in pdf]


class OpenAILoader(BaseLoader, ABC):
    """Generic OpenAI-compatible loader for multimodal OCR-style models."""

    PROMPT: str = ""  # To be defined by subclasses

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.llm = ChatOpenAI(
            base_url=self.config.loader.openai.base_url,
            api_key=self.config.loader.openai.api_key,
            model=self.config.loader.openai.model,
            temperature=self.config.loader.openai.temperature,
            timeout=self.config.loader.openai.timeout,
            max_retries=self.config.loader.openai.max_retries,
            top_p=self.config.loader.openai.top_p,
        )
        self.llm_semaphore = asyncio.Semaphore(self.config.loader.openai.concurrency_limit)

    async def aload_document(
        self,
        file_path: str | Path,
        metadata: dict | None = None,
        save_markdown: bool = False,
    ) -> Document:
        """Main pipeline: PDF → OCR → Caption → Markdown."""
        if metadata is None:
            metadata = {}

        start_time = time.time()
        file_path = str(file_path)

        try:
            pages = await pdf_to_images(file_path)
            ocr_results = await self._run_ocr_on_pages(pages)
            markdown = await self._assemble_markdown(pages, ocr_results)

            if save_markdown:
                self.save_content(markdown, file_path)

            duration = time.time() - start_time
            logger.info(f"Processed {file_path} in {duration:.2f}s")
            return Document(page_content=markdown, metadata=metadata)

        except Exception:
            logger.exception("Error in OpenAILoader.aload_document", path=file_path)
            raise

    async def _run_ocr_on_pages(self, pages: list[Image.Image]) -> list[dict]:
        tasks = [self._img2result(img) for img in pages]
        return await asyncio.gather(*tasks)

    async def _assemble_markdown(self, pages: list[Image.Image], results: list[dict]) -> str:
        markdown_parts = []
        for page_img, page_res in zip(pages, results):
            if not page_res:
                continue
            if self.image_captioning:
                await self._caption_images(page_img, page_res)
            markdown_parts.append(self._result_to_md(page_res))
        return "\n\n".join(markdown_parts).strip()

    async def _get_caption(self, img: Image.Image) -> str:
        try:
            return await self.get_image_description(image_data=img)
        except Exception as e:
            logger.warning(f"Captioning failed: {e}")
            return ""

    async def _img2result(self, img: Image.Image, format: str = "PNG") -> dict:
        """Send an image to the OpenAI-compatible OCR model."""
        async with self.llm_semaphore:
            try:
                buffer = io.BytesIO()
                img.save(buffer, format=format)
                img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/{format.lower()};base64,{img_b64}"},
                            },
                            {
                                "type": "text",
                                "text": f"<|img|><|imgpad|><|endofimg|>{self.PROMPT}",
                            },
                        ],
                    }
                ]

                response = await self.llm.ainvoke(messages)
                data = json.loads(response.content)
                return data

            except Exception as e:
                logger.error("Error in _img2result", error=str(e))
                return {}

    @abstractmethod
    def _result_to_md(self, result: list[dict]) -> str:
        """Convert structured OCR + caption results to markdown format."""

    @abstractmethod
    async def _caption_images(self, page_img: Image.Image, page_res: list):
        """Extract picture elements and caption them."""
