from PIL import Image
from tqdm.asyncio import tqdm
from utils.logger import logger  # assuming you have a shared logger instance
import io
import re
from .openai import OpenAILoader
import base64
from PIL import ImageOps

MODEL_PRESETS = {
    "Tiny":   {"base_size": 512,  "image_size": 512,  "crop_mode": False},
    "Small":  {"base_size": 640,  "image_size": 640,  "crop_mode": False},
    "Base":   {"base_size": 1024, "image_size": 1024, "crop_mode": False},
    "Large":  {"base_size": 1280, "image_size": 1280, "crop_mode": False},
    "Gundam": {"base_size": 1024, "image_size": 640,  "crop_mode": True},
}

class DeepSeekOCRLoader(OpenAILoader):
    """PDF loader using DeepSeek OCR"""

    PROMPT = "<image>\n<|grounding|>Convert the document to markdown."
    def __init__(self,mode: str = "Base", **kwargs):
        super().__init__(**kwargs)
        self.preset = MODEL_PRESETS.get(mode, MODEL_PRESETS["Base"])

    async def _img2result(self, img: Image.Image, format: str = "PNG") -> dict:
        """Send an image to the OpenAI-compatible OCR model."""
        img = self.preprocess_image(img)
        img_size = img.size
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
                return self._parse_deepseek_output(text_output, img_size)

            except Exception as e:
                logger.error("Error in _img2result", error=str(e))
                return {}

    def _parse_deepseek_output(self, text_output: str, img_size: tuple) -> list[dict]:
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
            bbox = self._normalize_bbox(bbox, img_size)

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
                    page_img = self.preprocess_image(page_img)
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
    from PIL import ImageOps

    def preprocess_image(self, img: Image.Image) -> Image.Image:
        """Resize and crop according to DeepSeek preset."""
        cfg = self.preset
        base_size  = cfg["base_size"]
        image_size = cfg["image_size"]
        crop_mode  = cfg["crop_mode"]

        img = ImageOps.exif_transpose(img)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')

        w, h = img.size
        # Resize so that the longest edge = base_size
        scale = base_size / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)

        if crop_mode:
            # Center-crop to image_size×image_size tiles
            left = max(0, (img.width  - image_size) // 2)
            top  = max(0, (img.height - image_size) // 2)
            right = left + image_size
            bottom = top + image_size
            img = img.crop((left, top, right, bottom))

        return img

    def _normalize_bbox(self, bbox, img_size):
        w, h = img_size
        return [
            int(bbox[0] / 999 * w),
            int(bbox[1] / 999 * h),
            int(bbox[2] / 999 * w),
            int(bbox[3] / 999 * h),
        ]