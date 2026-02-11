import asyncio
from io import BytesIO
from pathlib import Path

import cairosvg
from langchain_core.documents import Document
from PIL import Image, UnidentifiedImageError
from utils.logger import get_logger

from .base import BaseLoader

log = get_logger()


class ImageLoadError(Exception):
    """Raised when an image file cannot be loaded or converted."""


class ImageLoader(BaseLoader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _load_image(self, path: Path):
        """Load image file, converting SVG to PNG if needed."""
        if path.suffix.lower() == ".svg":
            png_data = cairosvg.svg2png(url=str(path))
            return Image.open(BytesIO(png_data))
        else:
            return Image.open(path)

    async def aload_document(self, file_path, metadata=None, save_markdown=False):
        path = Path(file_path)

        try:
            img = await asyncio.to_thread(self._load_image, path)
        except OSError as e:
            # File not found, permission denied, etc.
            log.error("Cannot read image file", file_path=str(path), error=str(e))
            raise ImageLoadError(f"Cannot read image file: {e}") from e
        except UnidentifiedImageError as e:
            # Invalid image format
            log.error("Invalid image format", file_path=str(path), error=str(e))
            raise ImageLoadError(f"Invalid image format: {e}") from e
        except Exception as e:
            # SVG conversion errors or other unexpected issues
            log.error(
                "Failed to load image file",
                file_path=str(path),
                error_type=type(e).__name__,
                error=str(e),
            )
            raise ImageLoadError(f"Cannot load image '{path.name}': {type(e).__name__}") from e

        description = await self.get_image_description(image_data=img)
        doc = Document(page_content=description, metadata=metadata)
        if save_markdown:
            await self.save_content(description, str(path))
        return doc
