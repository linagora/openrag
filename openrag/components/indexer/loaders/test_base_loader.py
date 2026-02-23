"""Unit tests for BaseLoader image conversion utilities."""

from unittest.mock import MagicMock

from PIL import Image

from .base import BaseLoader


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
