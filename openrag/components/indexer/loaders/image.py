from .base import BaseLoader
from PIL import Image
from pathlib import Path
from langchain_core.documents import Document


class ImageLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def aload_document(self, file_path, metadata=None, save_markdown=False):
        path = Path(file_path)
        img = Image.open(path)
        description = await self.get_image_description(image_data=img)
        doc = Document(page_content=description, metadata=metadata)
        if save_markdown:
            self.save_content(description, str(path))
        return doc
