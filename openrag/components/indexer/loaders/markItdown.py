import re
from io import BytesIO

from langchain_core.documents.base import Document
from markitdown import MarkItDown
from PIL import Image
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from .base import BaseLoader

logger = get_logger()


class MarkItDownLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.converter = MarkItDown()

    async def aload_document(self, file_path, metadata, save_markdown=False):
        result = self.converter.convert(file_path).text_content

        if self.image_captioning:
            images = self.get_images_from_zip(file_path)
            captions = await self.get_captions(images)
            for caption in captions:
                result = re.sub(
                    r"!\[[^!]*(\n){0,2}[^!]*\]\(data:image/.{0-6};base64...\)",
                    caption.replace("\\", "/"),
                    string=result,
                    count=1,
                )
        else:
            logger.info("Image captioning disabled. Ignoring images.")

        doc = Document(page_content=result, metadata=metadata)
        if save_markdown:
            self.save_content(result, str(file_path))
        return doc

    async def get_captions(self, images):
        tasks = [
            self.get_image_description(image_data_data_data=img)
            for img, image_ext in images
        ]
        return await tqdm.gather(*tasks, desc="Generating captions")

    def get_images_from_zip(self, input_file):
        import zipfile

        with zipfile.ZipFile(input_file, "r") as docx:
            file_names = docx.namelist()
            image_files = [f for f in file_names if f.startswith("word/media/")]

            images_not_in_order, order = (
                [],
                [],
            )  # the images got from the original file is not in the right order
            # but the target_ref contains the position of the image in the document

            for image_file in image_files:
                image_data = docx.read(image_file)
                image_extension = image_file.split(".")[-1].lower()
                image = Image.open(BytesIO(image_data))
                images_not_in_order.append((image, image_extension))
                order.append(
                    image_file.split("media/image")[1].split(f".{image_extension}")[0]
                )

            images = [1] * len(images_not_in_order)  # the images in the right order
            for i in range(len(images_not_in_order)):
                images[int(order[i]) - 1] = images_not_in_order[i]
            return images

    async def parse(self, file_path):
        result = await self.converter.convert(file_path)

        images = self.get_images_from_zip(file_path)
        captions = await self.get_captions(images)
        for caption in captions:
            result = re.sub(
                r"!\[[^!]*(\n){0,2}[^!]*\]\(data:image/.{0-6};base64...\)",
                caption.replace("\\", "/"),
                string=result,
                count=1,
            )
        return result
