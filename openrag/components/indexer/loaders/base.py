import base64
import re
from abc import ABC, abstractmethod
from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Union

from components.prompts import IMAGE_DESCRIBER
from components.utils import get_vlm_semaphore, load_config
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from PIL import Image
from utils.logger import get_logger

logger = get_logger()
config = load_config()


class BaseLoader(ABC):
    def __init__(self, **kwargs) -> None:
        self.page_sep = "[PAGE_SEP]"
        self.config = kwargs.get("config")
        settings: dict = dict(self.config.vlm)
        model_settings = {
            "temperature": 0.2,
            "max_retries": 3,
            "timeout": 60,
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        }
        settings.update(model_settings)

        self.image_captioning = self.config.loader.get("image_captioning", False)

        self.vlm_endpoint = ChatOpenAI(**settings).with_retry(stop_after_attempt=2)
        self.min_width_pixels = 0  # minimum width in pixels
        self.min_height_pixels = 0  # minimum height in pixels

    @abstractmethod
    async def aload_document(
        file_path: Union[str, Path],
        metadata: Optional[Dict] = None,
        save_markdown: bool = False,
    ):
        pass

    def save_content(self, text_content: str, path: str):
        path = re.sub(r"\..*", ".md", path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text_content)
        logger.debug(f"Document saved to {path}")

    def _pil_image_to_base64(self, image: Image.Image) -> str:
        """Convert PIL Image to base64 string."""
        buffered = BytesIO()
        # Determine format based on image mode or use PNG as default
        image.save(buffered, format="PNG")
        return base64.b64encode(buffered.getvalue()).decode()

    def _is_http_url(self, data: str) -> bool:
        """Check if string is an HTTP/HTTPS URL."""
        return isinstance(data, str) and data.startswith(("http://", "https://"))

    def _is_data_uri(self, data: str) -> bool:
        """Check if string is a data URI."""
        return isinstance(data, str) and data.startswith("data:image/")

    async def get_image_description(
        self,
        image_data: Union[Image.Image, str],
    ) -> str:
        """
        Creates a description for an image using the LLM model.

        Args:
            image_data: Can be one of:
                - PIL.Image object
                - str: HTTP/HTTPS URL
                - str: data URI (data:image/...;base64,...)
            semaphore: Semaphore to control access to the LLM model

        Returns:
            str: Description of the image wrapped in XML tags
        """
        async with get_vlm_semaphore():
            try:
                # Determine the type of image data and create appropriate message content
                if isinstance(image_data, Image.Image):
                    # logger.info("Processing PIL Image", img_size=str(image_data.size))
                    # Handle PIL Image
                    width, height = image_data.size

                    # Check minimum dimensions
                    if (
                        width <= self.min_width_pixels
                        or height <= self.min_height_pixels
                    ):
                        logger.debug(
                            f"Image too small: {width}x{height}, skipping description"
                        )

                    # Convert PIL Image to base64
                    img_b64 = self._pil_image_to_base64(image_data)
                    image_url = f"data:image/png;base64,{img_b64}"

                elif self._is_http_url(image_data):
                    # Handle HTTP/HTTPS URL
                    image_url = image_data
                    logger.debug(f"Processing HTTP URL: {image_data}")

                elif self._is_data_uri(image_data):
                    # Handle data URI - use as-is
                    image_url = image_data
                    logger.debug(f"Processing data URI: {image_data[:50]}...")

                else:
                    # Handle raw base64 string (assume it's base64 encoded image)
                    if isinstance(image_data, str):
                        try:
                            # Try to decode to verify it's valid base64
                            base64.b64decode(image_data)
                            image_url = f"data:image/png;base64,{image_data}"
                            logger.debug("Processing raw base64 string")
                        except Exception:
                            logger.error(
                                f"Invalid image data type or format: {type(image_data)}"
                            )
                            return """\n<image_description>\nInvalid image data format\n</image_description>\n"""
                    else:
                        logger.error(f"Unsupported image data type: {type(image_data)}")
                        return """\n<image_description>\nUnsupported image data type\n</image_description>\n"""

                # Create message for LLM
                message = HumanMessage(
                    content=[
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        },
                        {"type": "text", "text": IMAGE_DESCRIBER},
                    ]
                )

                # Get description from LLM
                response = await self.vlm_endpoint.ainvoke([message])
                image_description = response.content

            except Exception as e:
                logger.exception(
                    "Error while generating image description", error=str(e)
                )
                image_description = ""

            return f"""<image_description>\n\n{image_description}\n\n</image_description>"""
