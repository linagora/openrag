"""Unit tests for BaseLoader image conversion utilities."""

from unittest.mock import MagicMock

import pytest
from PIL import Image

from .base import BaseLoader, ensure_png_compatible_mode


class ConcreteLoader(BaseLoader):
    """Minimal concrete subclass for testing."""

    def __init__(self):
        # Skip BaseLoader.__init__ which needs config/VLM
        self.image_captioning = True

    async def aload_document(self, file_path, metadata=None, save_markdown=False):
        pass


class TestPilImageToBase64:
    def setup_method(self):
        self.loader = ConcreteLoader()

    def test_rgb_image(self):
        img = Image.new("RGB", (100, 100), "red")
        result = self.loader._pil_image_to_base64(img)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cmyk_image_converted(self):
        img = Image.new("CMYK", (100, 100), (0, 0, 0, 0))
        result = self.loader._pil_image_to_base64(img)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_rgba_image(self):
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        result = self.loader._pil_image_to_base64(img)
        assert isinstance(result, str)

    def test_palette_image(self):
        img = Image.new("P", (100, 100))
        result = self.loader._pil_image_to_base64(img)
        assert isinstance(result, str)

    def test_unsaveable_image_returns_empty(self):
        """Images that can't be converted at all return empty string."""
        img = MagicMock(spec=Image.Image)
        img.mode = "UNKNOWN"
        img.convert.side_effect = Exception("Cannot convert")
        img.save.side_effect = Exception("Cannot save")
        result = self.loader._pil_image_to_base64(img)
        assert result == ""


class TestGetImageDescription:
    def setup_method(self):
        self.loader = ConcreteLoader()

    @pytest.mark.asyncio
    async def test_small_image_skipped(self):
        """Images below minimum pixel threshold should be skipped without calling VLM."""
        small_img = Image.new("RGB", (10, 10), "red")
        result = await self.loader.get_image_description(small_img)
        assert "Image too small for captioning" in result

    def test_min_image_pixels_threshold(self):
        """Verify the threshold constant is set correctly."""
        assert BaseLoader.MIN_IMAGE_PIXELS == 784


class TestEnsurePngCompatibleMode:
    def test_cmyk_to_rgb(self):
        img = Image.new("CMYK", (10, 10))
        result = ensure_png_compatible_mode(img)
        assert result.mode == "RGB"

    def test_palette_to_rgba(self):
        img = Image.new("P", (10, 10))
        result = ensure_png_compatible_mode(img)
        assert result.mode == "RGBA"

    def test_rgb_unchanged(self):
        img = Image.new("RGB", (10, 10))
        result = ensure_png_compatible_mode(img)
        assert result.mode == "RGB"

    def test_rgba_unchanged(self):
        img = Image.new("RGBA", (10, 10))
        result = ensure_png_compatible_mode(img)
        assert result.mode == "RGBA"
