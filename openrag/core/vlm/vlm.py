"""Abstract VLM (Vision-Language Model) interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class VLM(ABC):
    """Base class for all vision-language model providers."""

    @abstractmethod
    async def caption_image(self, image_bytes: bytes, prompt: str | None = None) -> str:
        """Generate a caption/description for an image."""
        ...

    @abstractmethod
    async def caption_images_batch(self, images: list[bytes], prompt: str | None = None) -> list[str]:
        """Batch caption multiple images."""
        ...
