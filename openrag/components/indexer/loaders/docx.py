import re
import zipfile
from io import BytesIO

from langchain_core.documents.base import Document
from markitdown import MarkItDown
from PIL import Image
from tqdm.asyncio import tqdm
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
        # Pattern for HTTP/HTTPS images (linked images in docx)
        self._http_img_pattern = re.compile(r"!\[(.*?)\]\((https?://[^)]+)\)")

    async def aload_document(self, file_path, metadata, save_markdown=False):
        result = self.converter.convert(file_path).text_content

        if self.image_captioning:
            # Handle embedded images (data URIs)
            images = self.get_images_from_zip(file_path)

            captions = await self.get_captions(images)
            for caption in captions:
                result = re.sub(
                    r"!\[.*?\]\(data:image/.*?\)",
                    caption.replace("\\", "/"),
                    string=result,
                    count=1,
                )

            # Handle linked images (HTTP URLs) if enabled
            image_captioning_url = self.config.loader.get("image_captioning_url", False)
            http_matches = self._http_img_pattern.findall(result)

            if http_matches:
                if image_captioning_url:
                    tasks = {}
                    for alt, url in http_matches:
                        markdown_syntax = f"![{alt}]({url})"
                        tasks[markdown_syntax] = self.get_image_description(url)

                    if tasks:
                        url_captions = await tqdm.gather(*tasks.values(), desc="Captioning linked images")
                        url_captions_dict = dict(zip(tasks.keys(), url_captions))

                        for md_syntax, caption in url_captions_dict.items():
                            result = result.replace(md_syntax, caption)
                else:
                    logger.debug(
                        "Found HTTP image URLs but URL captioning is disabled",
                        http_images=len(http_matches),
                    )

        else:
            logger.info("Image captioning disabled. Ignoring images.")

        doc = Document(page_content=result, metadata=metadata)
        if save_markdown:
            self.save_content(result, str(file_path))
        return doc

    async def get_captions(self, images):
        tasks = [self.get_image_description(image_data=img) for img in images]
        return await tqdm.gather(*tasks, desc="Generating captions")

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
