from abc import ABC, abstractmethod
from typing import Any


class VLM(ABC):
    """Abstract interface for Vision-Language Model operations.

    Currently embedded in BaseLoader (components/indexer/loaders/base.py).
    This ABC extracts the VLM contract for independent use.
    """

    @abstractmethod
    async def describe_image(self, image_data: Any) -> str:
        """Generate a text description for an image.

        Args:
            image_data: PIL Image, HTTP URL string, data URI string,
                        or base64-encoded string.

        Returns:
            Description text for the image.
        """
        ...

    @abstractmethod
    async def caption_images(
        self, images: list[Any], desc: str = ""
    ) -> list[str]:
        """Generate captions for a batch of images concurrently.

        Args:
            images: List of PIL Image objects.
            desc: Progress bar description.

        Returns:
            List of caption strings, one per image.
        """
        ...
