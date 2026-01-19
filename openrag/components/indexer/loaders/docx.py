import re
import zipfile
from io import BytesIO

from langchain_core.documents.base import Document
from markitdown import MarkItDown
from PIL import Image
from utils.logger import get_logger

from .base import BaseLoader

logger = get_logger()


def convert_to_png_image(image: Image.Image) -> Image.Image:
    # Save the image into a BytesIO buffer in PNG format
    with BytesIO() as buffer:
        image.save(buffer, format="PNG")
        buffer.seek(0)
        # Reload the image from the buffer as a PNG
        png_image = Image.open(buffer).convert("RGBA")
    return png_image


class DocxLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.converter = MarkItDown()

    async def aload_document(self, file_path, metadata, save_markdown=False):
        result = self.converter.convert(file_path).text_content

        if self.image_captioning:
            # Handle embedded images (extracted from docx zip)
            images = self.get_images_from_zip(file_path)
            captions = await self.caption_images(images, desc="Captioning embedded images")
            for caption in captions:
                result = re.sub(
                    r"!\[.*?\]\(data:image/.*?\)",
                    caption.replace("\\", "/"),
                    string=result,
                    count=1,
                )

            # Handle linked images (HTTP URLs) using shared method
            # Only caption HTTP URLs, data URIs are already handled above
            result = await self.replace_markdown_images_with_captions(
                result,
                caption_data_uris=False,
                desc="Captioning linked images",
            )
        else:
            logger.info("Image captioning disabled. Ignoring images.")

        doc = Document(page_content=result, metadata=metadata)
        if save_markdown:
            self.save_content(result, str(file_path))
        return doc

    def get_images_from_zip(self, input_file):
        with zipfile.ZipFile(input_file, "r") as docx:
            file_names = docx.namelist()
            image_files = [f for f in file_names if f.startswith("word/media/")]
            if not image_files:
                return []

            images_not_in_order, order = [], []

            # the images got from the original file is not in the right order
            # but the target_ref contains the position of the image in the document

            for image_file in image_files:
                image_data = docx.read(image_file)
                image_extension = image_file.split(".")[-1].lower()
                image = Image.open(BytesIO(image_data))

                # Convert to PNG-compatible format
                image = convert_to_png_image(image)

                images_not_in_order.append(image)
                order.append(image_file.split("media/image")[1].split(f".{image_extension}")[0])

            images = [None] * len(images_not_in_order)  # the images in the right order
            for i in range(len(images_not_in_order)):
                images[int(order[i]) - 1] = images_not_in_order[i]
            return images
