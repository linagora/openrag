from PIL import Image
from tqdm.asyncio import tqdm
from utils.logger import logger  # assuming you have a shared logger instance
import io
import re
from .openai import OpenAILoader
import base64


class DeepSeekOCRLoader(OpenAILoader):
    """PDF loader using DeepSeek OCR"""

    PROMPT = "<image>\n<|grounding|>Convert the document to markdown."
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

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
                                "image_url": {
                                    "url": f"data:image/{format.lower()};base64,{img_b64}"
                                },
                            },
                            {
                                "type": "text",
                                "text": self.PROMPT,
                            },
                        ],
                    }
                ]

                response = await self.llm.ainvoke(messages)
                text_output = response.content.strip()
                return self._parse_deepseek_output(text_output)

            except Exception as e:
                logger.error("Error in _img2result", error=str(e))
                return {}

    def _parse_deepseek_output(self, text_output: str) -> list[dict]:
        """
        Parse DeepSeek OCR output of the form:
        text[[x1, y1, x2, y2]]
        Some content...

        image[[x1, y1, x2, y2]]
        """
        elements = []
        # capture lines like: text[[141, 186, 335, 212]] + next line text (optional)
        pattern = re.compile(
            r"(?P<category>text|image)\s*\[\[(?P<bbox>[0-9,\s]+)\]\]\s*(?P<content>[^\n]*)",
            re.IGNORECASE,
        )

        for match in pattern.finditer(text_output):
            cat = match.group("category").lower()
            bbox_str = match.group("bbox")
            bbox = [int(x.strip()) for x in bbox_str.split(",")]

            if cat == "image":
                elements.append({"bbox": bbox, "category": "Picture"})
            else:
                text_content = match.group("content").strip()
                if text_content:
                    elements.append(
                        {"bbox": bbox, "category": "Text", "text": text_content}
                    )

        return elements

    async def _caption_images(self, page_img: Image.Image, page_res: list):
        """Extract picture elements and caption them."""
        picture_items = [item for item in page_res if item.get("category") == "Picture"]
        if not picture_items:
            return

        picture_crops = []
        for item in picture_items:
            bbox = item.get("bbox")
            if bbox and len(bbox) == 4:
                try:
                    cropped = page_img.crop(bbox)
                    picture_crops.append((item, cropped))
                except Exception as e:
                    logger.warning(f"Failed to crop image bbox {bbox}: {e}")

        if picture_crops:
            desc_tasks = [self._get_caption(crop) for _, crop in picture_crops]
            desc_results = await tqdm.gather(
                *desc_tasks,
                desc="Captioning images",
                total=len(desc_tasks),
            )
            for (item, _), desc in zip(picture_crops, desc_results):
                item["text"] = desc.strip() if isinstance(desc, str) else ""

    def _result_to_md(self, result: list[dict]) -> str:
        return "\n".join(
            item.get("text", "").strip() for item in result if item.get("text")
        )
