from PIL import Image
from tqdm.asyncio import tqdm
from utils.logger import logger  # assuming you have a shared logger instance

from .openai import OpenAILoader


class DotsOCRLoader(OpenAILoader):
    """PDF loader using DotsOCR"""

    PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

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
        return "\n".join(item.get("text", "").strip() for item in result if item.get("text"))
